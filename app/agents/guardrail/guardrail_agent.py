import logging
import uuid
from typing import Any, Dict, List
from motor.motor_asyncio import AsyncIOMotorClient

from app.config.settings import settings
from app.graph.state import Finding, TestRunState
from app.tools.embeddings import EmbeddingTool
from app.tools.rag_retriever import get_chroma_client
from app.utils.datetime import current_utc_iso

logger = logging.getLogger("guardrail_agent")

DEDUP_SIMILARITY_THRESHOLD = 0.85


async def guardrail_agent_node(state: TestRunState) -> Dict[str, Any]:
    """Guardrail Agent: Filters, deduplicates, and routes findings based on deterministic rules."""
    logger.info("[GUARDRAIL_AGENT] Executing guardrail checks on findings...")

    findings: List[Finding] = state.get("findings", [])
    run_id = state.get("run_id")
    test_cases = {tc["id"]: tc for tc in state.get("test_cases", [])}

    if not findings:
        logger.info("[GUARDRAIL_AGENT] No findings to process.")
        return {"status": "completed"}

    from app.database import get_mongo_db
    db = await get_mongo_db()

    spec_id = ""
    try:
        run_doc = await db["runs"].find_one({"_id": run_id})
        if run_doc:
            spec_id = run_doc.get("spec_id", "")
    except Exception as e:
        logger.warning(f"[GUARDRAIL_AGENT] Failed to fetch run spec_id: {e}")

    processed_findings: List[Finding] = []
    pending_human_review: List[str] = []
    auto_approved_defects: List[Dict[str, Any]] = []

    # Get Chroma client for deduplication search
    chroma_client = get_chroma_client()
    defects_vector_col = None
    try:
        defects_vector_col = chroma_client.get_or_create_collection("defects")
    except Exception as ex:
        logger.warning(f"[GUARDRAIL_AGENT] Failed to access defects vector collection: {ex}. Deduplication will fail closed.")

    min_confidence = getattr(settings, "MIN_CONFIDENCE_THRESHOLD", 0.6)

    for finding in findings:
        finding_id = finding["id"]
        tc_id = finding["test_case_id"]
        tc = test_cases.get(tc_id, {})
        tc_name = tc.get("name", "Unknown Test Case")
        op_id = tc.get("endpoint_operation_id", "unknown_operation")

        # 1. Confidence Threshold check
        if finding.get("confidence", 0.0) < min_confidence:
            logger.info(
                f"[GUARDRAIL_AGENT] Discarding finding '{finding_id}' due to low confidence: "
                f"{finding.get('confidence')} < {min_confidence}"
            )
            finding["guardrail_status"] = "rejected"
            finding["is_duplicate"] = False
            
            # Save discarded finding to Mongo
            await db["findings"].replace_one({"_id": finding_id}, {**finding, "run_id": run_id}, upsert=True)
            processed_findings.append(finding)
            continue

        # 2. Semantic Deduplication check (embedding match against existing defects)
        is_duplicate = False
        duplicate_of = None
        finding_vector = None

        if defects_vector_col and finding.get("root_cause_summary"):
            try:
                # Generate embedding of root_cause_summary
                embed_res = await EmbeddingTool.embed(finding["root_cause_summary"])
                if embed_res.success and embed_res.data:
                    finding_vector = embed_res.data
                    
                    if defects_vector_col.count() > 0:
                        query_res = defects_vector_col.query(
                            query_embeddings=[finding_vector],
                            n_results=1
                        )
                        if query_res and query_res.get("documents") and query_res["documents"][0]:
                            matched_id = query_res["ids"][0][0]
                            distance = query_res["distances"][0][0]
                            similarity = 1.0 - float(distance)

                            if similarity >= DEDUP_SIMILARITY_THRESHOLD:
                                logger.info(
                                    f"[GUARDRAIL_AGENT] Finding '{finding_id}' is a semantic duplicate of "
                                    f"existing defect '{matched_id}' (similarity: {similarity:.4f} >= {DEDUP_SIMILARITY_THRESHOLD})"
                                )
                                is_duplicate = True
                                duplicate_of = matched_id
            except Exception as e:
                # Fail closed on vector DB / embedding failures
                logger.warning(f"[GUARDRAIL_AGENT] Deduplication check failed for finding '{finding_id}': {e}. Failing closed.")
                is_duplicate = False

        finding["is_duplicate"] = is_duplicate
        if is_duplicate:
            finding["guardrail_status"] = "rejected"
            finding["duplicate_of"] = duplicate_of
            # Save duplicate finding to Mongo
            await db["findings"].replace_one({"_id": finding_id}, {**finding, "run_id": run_id}, upsert=True)
            processed_findings.append(finding)
            continue

        # 3. Severity Routing
        severity = finding.get("severity", "medium")
        if severity in ("critical", "high"):
            finding["guardrail_status"] = "pending_review"
            pending_human_review.append(finding_id)
            logger.info(f"[GUARDRAIL_AGENT] Finding '{finding_id}' routed to human review (severity: {severity}).")
        else:
            finding["guardrail_status"] = "auto_approved"
            logger.info(f"[GUARDRAIL_AGENT] Finding '{finding_id}' auto-approved (severity: {severity}).")

            # Persist Defect immediately
            defect_id = f"def_{uuid.uuid4().hex[:8]}"
            defect_doc = {
                "_id": defect_id,
                "run_id": run_id,
                "spec_id": spec_id,
                "test_case_id": tc_id,
                "finding_id": finding_id,
                "severity": severity,
                "confidence": finding["confidence"],
                "status": "open",
                "title": f"Defect in {tc_name} ({op_id})",
                "root_cause_summary": finding["root_cause_summary"],
                "suggested_fix": finding["suggested_fix"],
                "supporting_evidence": finding["supporting_evidence"],
                "external_ref": None,
                "created_at": current_utc_iso()
            }
            await db["defects"].insert_one(defect_doc)
            logger.info(f"[GUARDRAIL_AGENT] Created auto-approved defect '{defect_id}' for finding '{finding_id}'.")

            # Also index new defect in vector DB to allow future dedup matching
            if defects_vector_col and finding_vector:
                try:
                    defects_vector_col.upsert(
                        ids=[defect_id],
                        documents=[finding["root_cause_summary"]],
                        embeddings=[finding_vector]
                    )
                except Exception as ex:
                    logger.warning(f"[GUARDRAIL_AGENT] Failed to index defect '{defect_id}' in vector store: {ex}")

        # Save finding to Mongo
        await db["findings"].replace_one({"_id": finding_id}, {**finding, "run_id": run_id}, upsert=True)
        processed_findings.append(finding)

    # 4. Return routing updates
    if pending_human_review:
        return {
            "findings": processed_findings,
            "pending_human_review": pending_human_review,
            "status": "awaiting_human"
        }
    else:
        return {
            "findings": processed_findings,
            "pending_human_review": [],
            "status": "completed"
        }
