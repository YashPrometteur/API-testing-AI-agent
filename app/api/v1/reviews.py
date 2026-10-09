import logging
import uuid
from typing import Any, Dict, Optional, Literal
from fastapi import APIRouter, HTTPException, status, Request
from pydantic import BaseModel, Field

from app.config.settings import settings
from app.graph.build import build_graph
from app.graph.checkpointer import MongoCheckpointSaver
from app.repositories.finding_repository import FindingRepository
from app.repositories.defect_repository import DefectRepository
from app.repositories.run_repository import RunRepository
from app.tools.embeddings import EmbeddingTool
from app.tools.rag_retriever import get_chroma_client
from app.utils.datetime import current_utc_iso

router = APIRouter()


class ReviewPayload(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str
    notes: Optional[str] = ""


@router.get("/pending")
async def get_pending_reviews(run_id: Optional[str] = None):
    """List all findings currently pending human review, optionally filtered by run_id."""
    from app.database import get_mongo_db
    db = await get_mongo_db()
    finding_repo = FindingRepository(db)

    if run_id:
        findings = await finding_repo.find_pending_review_by_run(run_id)
    else:
        cursor = db["findings"].find({"guardrail_status": "pending_review"})
        findings = await cursor.to_list(100)

    return {
        "pending_count": len(findings),
        "pending_reviews": findings
    }


@router.patch("/{defect_id}/review")
async def review_defect(defect_id: str, payload: ReviewPayload):
    """Review a pending finding (addressed via finding ID find_... or defect ID def_...)."""
    from app.database import get_mongo_db
    db = await get_mongo_db()

    finding_repo = FindingRepository(db)
    defect_repo = DefectRepository(db)
    run_repo = RunRepository(db)

    # 1. Fetch finding by ID (or resolve via defect record if def_ prefix used)
    finding = await finding_repo.find_by_id(defect_id)
    if not finding and defect_id.startswith("def_"):
        defect_doc = await defect_repo.find_by_id(defect_id)
        if defect_doc and defect_doc.get("finding_id"):
            finding = await finding_repo.find_by_id(defect_doc["finding_id"])

    if not finding:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pending defect/finding with id '{defect_id}' not found."
        )

    target_finding_id = finding["_id"]

    # 2. Check if already decided
    if finding.get("guardrail_status") != "pending_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Defect review already decided. Current status: '{finding.get('guardrail_status')}'."
        )

    run_id = finding["run_id"]
    decision = payload.decision
    new_status = "approved" if decision == "approve" else "rejected"

    # 3. Update finding status in DB
    await finding_repo.update(target_finding_id, {
        "guardrail_status": new_status,
        "reviewer": payload.reviewer,
        "review_notes": payload.notes,
        "reviewed_at": current_utc_iso()
    })

    created_defect_id = None

    # 4. If approved, persist to defects collection
    if decision == "approve":
        # Load run state from checkpoint to get test case metadata
        tc_name = "Unknown Test Case"
        op_id = "unknown_operation"
        try:
            checkpointer = MongoCheckpointSaver(db=db)
            graph = build_graph(checkpointer=checkpointer)
            config = {"configurable": {"thread_id": run_id}}
            state_snapshot = await graph.aget_state(config)
            if state_snapshot and state_snapshot.values:
                test_cases = {tc["id"]: tc for tc in state_snapshot.values.get("test_cases", [])}
                tc = test_cases.get(finding["test_case_id"], {})
                tc_name = tc.get("name", "Unknown Test Case")
                op_id = tc.get("endpoint_operation_id", "unknown_operation")
        except Exception as e:
            logger.warning(f"[REVIEWS_API] Failed to fetch test case details from checkpoint: {e}")

        spec_id = ""
        try:
            run_doc = await run_repo.find_by_id(run_id)
            if run_doc:
                spec_id = run_doc.get("spec_id", "")
        except Exception as e:
            logger.warning(f"[REVIEWS_API] Failed to fetch run spec_id: {e}")

        created_defect_id = f"def_{uuid.uuid4().hex[:8]}"
        defect_doc = {
            "_id": created_defect_id,
            "run_id": run_id,
            "spec_id": spec_id,
            "test_case_id": finding["test_case_id"],
            "finding_id": target_finding_id,
            "severity": finding["severity"],
            "confidence": finding["confidence"],
            "status": "open",
            "title": f"Defect in {tc_name} ({op_id})",
            "root_cause_summary": finding["root_cause_summary"],
            "suggested_fix": finding["suggested_fix"],
            "supporting_evidence": finding.get("supporting_evidence", []),
            "external_ref": None,
            "created_at": current_utc_iso()
        }
        await defect_repo.insert_defect(defect_doc)

        # Index defect root_cause_summary in Chroma/fallback defects collection
        try:
            chroma_client = get_chroma_client()
            defects_vector_col = chroma_client.get_or_create_collection("defects")
            embed_res = await EmbeddingTool.embed(finding["root_cause_summary"])
            if embed_res.success and embed_res.data:
                defects_vector_col.upsert(
                    ids=[created_defect_id],
                    documents=[finding["root_cause_summary"]],
                    embeddings=[embed_res.data]
                )
        except Exception as ex:
            logger.warning(f"[REVIEWS_API] Failed to index approved defect '{created_defect_id}' in vector store: {ex}")

    # 5. Check if other findings are still pending review for this run
    pending_findings = await finding_repo.find_pending_review_by_run(run_id)
    run_resumed = False

    if not pending_findings:
        # Resume the graph!
        logger.info(f"[REVIEWS_API] All pending reviews resolved for run '{run_id}'. Resuming execution graph...")
        checkpointer = MongoCheckpointSaver(db=db)
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": run_id}}
        
        # Resume with Command(resume=...) so LangGraph advances past interrupt() to report_stub
        from langgraph.types import Command
        graph_res = await graph.ainvoke(
            Command(resume={"reviewed_by": payload.reviewer, "decision": decision}),
            config=config
        )

        final_status = "completed" if (graph_res and ("final_report" in graph_res or graph_res.get("status") == "completed")) else (graph_res.get("status") if graph_res else "completed")
        update_data: Dict[str, Any] = {
            "endpoints": graph_res.get("endpoints", []) if graph_res else [],
            "test_plan": graph_res.get("test_plan", {}) if graph_res else {},
            "errors": graph_res.get("errors", []) if graph_res else [],
            "updated_at": current_utc_iso(),
        }
        if graph_res and "test_case_executions" in graph_res:
            update_data["test_case_executions"] = graph_res["test_case_executions"]
        if graph_res and "final_report" in graph_res:
            update_data["final_report"] = graph_res["final_report"]

        await run_repo.update_status(run_id, final_status, update_data)
        from app.database import save_mock_db_to_disk
        await save_mock_db_to_disk()

        logger.info(
            f"[REVIEWS_API] Run '{run_id}' resumed from guardrail_interrupt and completed. "
            f"Persisted {len(update_data.get('test_case_executions', []))} test case executions and final_report."
        )
        run_resumed = True

    return {
        "run_resumed": run_resumed,
        "defect_id": created_defect_id or target_finding_id,
        "status": new_status
    }


class BulkReviewPayload(BaseModel):
    reviewer: str = "qa_lead"
    notes: Optional[str] = "Bulk approved via 1-click action"


@router.post("/runs/{run_id}/approve-all")
@router.post("/runs/{run_id}/bulk-approve")
async def bulk_approve_reviews(run_id: str, request: Request):
    """Approve all pending reviews for a specific run in a single action and resume execution."""
    reviewer = "qa_lead"
    notes = "Bulk approved via 1-click action"

    if request:
        try:
            body_bytes = await request.body()
            if body_bytes:
                import json
                try:
                    raw_data = json.loads(body_bytes.decode("utf-8"))
                    if isinstance(raw_data, str):
                        raw_data = json.loads(raw_data)
                    if isinstance(raw_data, dict):
                        reviewer = raw_data.get("reviewer", reviewer)
                        notes = raw_data.get("notes", notes)
                except Exception:
                    pass
        except Exception:
            pass

    payload = BulkReviewPayload(reviewer=reviewer, notes=notes)

    from app.database import get_mongo_db
    db = await get_mongo_db()

    finding_repo = FindingRepository(db)
    defect_repo = DefectRepository(db)
    run_repo = RunRepository(db)

    pending_findings = await finding_repo.find_pending_review_by_run(run_id)
    if not pending_findings:
        return {
            "run_id": run_id,
            "approved_count": 0,
            "message": f"No pending reviews found for run '{run_id}'.",
            "run_resumed": False
        }

    tc_map: Dict[str, Any] = {}
    spec_id = ""
    try:
        run_doc = await run_repo.find_by_id(run_id)
        if run_doc:
            spec_id = run_doc.get("spec_id", "")
        checkpointer = MongoCheckpointSaver(db=db)
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": run_id}}
        state_snapshot = await graph.aget_state(config)
        if state_snapshot and state_snapshot.values:
            tc_map = {tc["id"]: tc for tc in state_snapshot.values.get("test_cases", [])}
    except Exception as e:
        logger.warning(f"[BULK_REVIEW] Failed to fetch checkpoint details: {e}")

    chroma_client = None
    defects_vector_col = None
    try:
        chroma_client = get_chroma_client()
        defects_vector_col = chroma_client.get_or_create_collection("defects")
    except Exception as ex:
        logger.warning(f"[BULK_REVIEW] Vector collection access skipped: {ex}")

    approved_count = 0
    created_defects: List[str] = []

    for finding in pending_findings:
        fid = finding["_id"]
        tc_id = finding.get("test_case_id", "")
        tc = tc_map.get(tc_id, {})
        tc_name = tc.get("name", "Unknown Test Case")
        op_id = tc.get("endpoint_operation_id", "unknown_operation")

        await finding_repo.update(fid, {
            "guardrail_status": "approved",
            "reviewer": payload.reviewer,
            "review_notes": payload.notes,
            "reviewed_at": current_utc_iso()
        })

        defect_id = f"def_{uuid.uuid4().hex[:8]}"
        defect_doc = {
            "_id": defect_id,
            "run_id": run_id,
            "spec_id": spec_id,
            "test_case_id": tc_id,
            "finding_id": fid,
            "severity": finding.get("severity", "high"),
            "confidence": finding.get("confidence", 0.9),
            "status": "open",
            "title": f"Defect in {tc_name} ({op_id})",
            "root_cause_summary": finding.get("root_cause_summary", ""),
            "suggested_fix": finding.get("suggested_fix", ""),
            "supporting_evidence": finding.get("supporting_evidence", []),
            "external_ref": None,
            "created_at": current_utc_iso()
        }
        await defect_repo.insert_defect(defect_doc)
        created_defects.append(defect_id)

        if defects_vector_col and finding.get("root_cause_summary"):
            try:
                embed_res = await EmbeddingTool.embed(finding["root_cause_summary"])
                if embed_res.success and embed_res.data:
                    defects_vector_col.upsert(
                        ids=[defect_id],
                        documents=[finding["root_cause_summary"]],
                        embeddings=[embed_res.data]
                    )
            except Exception as ex:
                logger.debug(f"[BULK_REVIEW] Vector index skipped for defect {defect_id}: {ex}")

        approved_count += 1

    # Resume graph execution
    logger.info(f"[BULK_REVIEW] Bulk-approved {approved_count} findings for run '{run_id}'. Resuming execution graph...")
    checkpointer = MongoCheckpointSaver(db=db)
    graph = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": run_id}}

    from langgraph.types import Command
    graph_res = await graph.ainvoke(
        Command(resume={"reviewed_by": payload.reviewer, "decision": "approve", "bulk": True}),
        config=config
    )

    final_status = "completed" if (graph_res and ("final_report" in graph_res or graph_res.get("status") == "completed")) else (graph_res.get("status") if graph_res else "completed")
    update_data: Dict[str, Any] = {
        "endpoints": graph_res.get("endpoints", []) if graph_res else [],
        "test_plan": graph_res.get("test_plan", {}) if graph_res else {},
        "errors": graph_res.get("errors", []) if graph_res else [],
        "updated_at": current_utc_iso(),
    }
    if graph_res and "test_case_executions" in graph_res:
        update_data["test_case_executions"] = graph_res["test_case_executions"]
    if graph_res and "final_report" in graph_res:
        update_data["final_report"] = graph_res["final_report"]

    await run_repo.update_status(run_id, final_status, update_data)
    from app.database import save_mock_db_to_disk
    await save_mock_db_to_disk()

    return {
        "run_id": run_id,
        "approved_count": approved_count,
        "created_defects": created_defects,
        "run_resumed": True,
        "status": final_status
    }

