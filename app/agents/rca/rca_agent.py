import asyncio
import logging
import os
import uuid
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from app.config.settings import settings
from app.graph.state import Finding, TestRunState
from app.llm import get_llm_client
from app.tools.rag_retriever import RAGRetrieverTool

logger = logging.getLogger("rca_agent")


class RCADiagnosis(BaseModel):
    severity: Literal["low", "medium", "high", "critical"] = Field(
        description="Severity classification of the failure."
    )
    confidence: float = Field(
        description="Diagnostic confidence between 0.0 and 1.0 based on evidence match."
    )
    root_cause_summary: str = Field(
        description="Developer-friendly explanation of why the failure occurred."
    )
    suggested_fix: str = Field(
        description="Code-level recommendation or architectural fix to solve the defect."
    )
    supporting_evidence: List[str] = Field(
        description="List of document IDs or past defect IDs from the search context used as evidence."
    )


def load_rca_prompt() -> str:
    """Load system prompt for RCA analysis agent."""
    path = os.path.join(os.getcwd(), "app/prompts/rca/rca_v1.md")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"[RCA_AGENT] Failed to read prompt file: {e}")
    return "You are a precise API diagnostic agent."


def normalize_severity(
    proposed_severity: str,
    category: str,
    actual_status_code: Optional[int],
    expected_status_code: Optional[int] = None
) -> str:
    """
    Deterministic rule engine to normalize LLM-assigned severity:
    - 4xx status mismatches on non-security categories (functional, negative, edge_case)
      such as expected 400, actual 404/405/415 are normalized to 'medium'.
    - Server crashes (>= 500) and security categories retain 'high' or 'critical'.
    """
    sev = (proposed_severity or "medium").lower()
    cat = (category or "").lower()

    if actual_status_code and 400 <= actual_status_code < 500:
        if cat in ("functional", "negative", "edge_case", "regression"):
            if sev in ("high", "critical"):
                logger.info(
                    f"[RCA_AGENT] Normalizing severity from '{sev}' to 'medium' for 4xx client status mismatch "
                    f"(actual: {actual_status_code}, category: '{cat}')."
                )
                return "medium"

    return sev if sev in ("low", "medium", "high", "critical") else "medium"


async def rca_node(state: TestRunState) -> Dict[str, Any]:
    """RCA Node: Performs root cause diagnosis on failing execution results."""
    logger.info("[RCA_AGENT] Running Root Cause Analysis on execution results...")

    execution_results = state.get("execution_results", [])
    test_cases = {tc["id"]: tc for tc in state.get("test_cases", [])}
    existing_findings = state.get("findings", [])
    existing_by_tc_id = {
        f["test_case_id"]: f
        for f in existing_findings
        if isinstance(f, dict) and "test_case_id" in f
    }
    findings: List[Finding] = []

    # Filter failing, erroring, or assertion-failed execution results
    failing_results = [
        res for res in execution_results
        if res.get("status") in ("fail", "error") or (res.get("status") == "flaky" and res.get("assertion_failures"))
    ]

    if not failing_results:
        logger.info("[RCA_AGENT] No failing test cases found. Skipping RCA.")
        return {"findings": [], "status": "running"}

    llm_client = get_llm_client()
    system_prompt = load_rca_prompt()

    for res in failing_results:
        tc_id = res["test_case_id"]
        tc = test_cases.get(tc_id, {})
        actual_code = res.get("actual_status_code")
        actual_body = res.get("actual_body")
        failures = res.get("assertion_failures", [])

        # Failure signature for current execution
        current_failure_reason = "; ".join(failures) if failures else str(actual_body or "")

        # If a pending finding already exists for this testcase, check if the failure signature changed
        existing_finding = existing_by_tc_id.get(tc_id)
        if existing_finding and existing_finding.get("guardrail_status") == "pending":
            old_status = existing_finding.get("diagnosed_status_code")
            old_reason = existing_finding.get("diagnosed_failure_reason")

            # Check if failure signature is identical
            if old_status is not None and old_status == actual_code and old_reason == current_failure_reason:
                finding_id = existing_finding.get("id")
                logger.info(f"[RCA_AGENT] Reusing existing finding_id '{finding_id}' for test case '{tc_id}' (failure signature unchanged).")
                findings.append(existing_finding)
                continue
            elif old_status is None and old_reason is None:
                # Legacy finding without signature metadata — reuse unchanged
                finding_id = existing_finding.get("id")
                logger.info(f"[RCA_AGENT] Reusing existing finding_id '{finding_id}' for test case '{tc_id}' (previously diagnosed).")
                findings.append(existing_finding)
                continue
            else:
                # Failure signature changed! Re-diagnose with LLM but keep the SAME finding_id
                finding_id = existing_finding.get("id")
                logger.info(
                    f"[RCA_AGENT] Failure signature changed for '{tc_id}' "
                    f"(was {old_status}, now {actual_code}) — re-diagnosing finding '{finding_id}'."
                )
        else:
            finding_id = f"find_{uuid.uuid4().hex[:8]}"
            logger.info(f"[RCA_AGENT] Minting new finding '{finding_id}' for test case '{tc_id}'.")

        # Retrieve semantic context from RAG vector DB
        query = f"Endpoint: {tc.get('endpoint_operation_id')}. Path: {tc.get('request', {}).get('path')}. Failures: {', '.join(failures)}"
        rag_res = await RAGRetrieverTool.retrieve(query=query, top_k=3)
        context_docs = rag_res.data or []

        # Format context for prompt
        context_str = ""
        if context_docs:
            context_str = "\n".join(
                f"- ID: {doc_id} | Similarity: {sim:.2f} | Content: {content}"
                for content, doc_id, sim in context_docs
            )
        else:
            context_str = "No similar defects or standard documents found."

        prompt = (
            f"Please diagnose the following API execution failure:\n\n"
            f"### Test Case details\n"
            f"- ID: {tc_id}\n"
            f"- Name: {tc.get('name')}\n"
            f"- Endpoint: {tc.get('endpoint_operation_id')}\n"
            f"- Category: {tc.get('category')}\n"
            f"- Expected: {tc.get('expected')}\n"
            f"- Request Path: {tc.get('request', {}).get('path')}\n"
            f"- Request Method: {tc.get('request', {}).get('method')}\n"
            f"- Request Body: {tc.get('request', {}).get('body')}\n\n"
            f"### Actual Response details\n"
            f"- Status Code: {actual_code}\n"
            f"- Response Body: {actual_body}\n"
            f"- Assertion Failures: {failures}\n\n"
            f"### Retrieved Context:\n"
            f"{context_str}\n"
        )

        diagnosis = None
        # Try structured generation with 1 re-prompt on validation failure
        for attempt in range(2):
            try:
                diagnosis = await llm_client.generate_structured(
                    prompt=prompt,
                    response_model=RCADiagnosis,
                    system_prompt=system_prompt,
                    temperature=0.1
                )
                break
            except Exception as e:
                logger.warning(f"[RCA_AGENT] Structured output attempt {attempt + 1} failed for {tc_id}: {e}")
                if attempt == 0:
                    prompt += f"\n\nCorrection Note: Previous attempt failed validation. Please adhere strictly to the JSON schema output format."
                    await asyncio.sleep(1)

        # Build Finding entry (fails closed/safe)
        expected_code = tc.get("expected", {}).get("status_code") if isinstance(tc.get("expected"), dict) else None
        
        if diagnosis:
            normalized_sev = normalize_severity(
                proposed_severity=diagnosis.severity,
                category=tc.get("category", ""),
                actual_status_code=actual_code,
                expected_status_code=expected_code
            )
            finding: Finding = {
                "id": finding_id,
                "test_case_id": tc_id,
                "severity": normalized_sev,
                "confidence": diagnosis.confidence,
                "root_cause_summary": diagnosis.root_cause_summary,
                "suggested_fix": diagnosis.suggested_fix,
                "supporting_evidence": diagnosis.supporting_evidence,
                "is_duplicate": False,
                "guardrail_status": "pending",
                "diagnosed_status_code": actual_code,
                "diagnosed_failure_reason": current_failure_reason
            }
        else:
            # Deterministic, evidence-based fallback diagnosis when LLM call is unavailable
            api_msg = ""
            if isinstance(actual_body, dict):
                api_msg = str(actual_body.get("message") or actual_body.get("detail") or "")
            elif actual_body:
                api_msg = str(actual_body)

            api_msg_clean = api_msg.strip()
            msg_lower = api_msg_clean.lower()

            if "already exists" in msg_lower:
                rc_summary = f"Conflict constraint violation: The API returned HTTP {actual_code} ('{api_msg_clean}') because an active resource already exists."
                sug_fix = "Deactivate existing active plans of the same duration before activating a new plan, or adjust business logic to allow multiple active plans."
            elif "not found" in msg_lower:
                rc_summary = f"Resource missing or inactive: The API returned HTTP {actual_code} ('{api_msg_clean}') indicating the target entity was not found or is inactive."
                sug_fix = f"Ensure the target entity ID exists and is active in the database before querying details for {tc.get('endpoint_operation_id', 'endpoint')}."
            elif actual_code == 400 and expected_code == 403:
                rc_summary = f"Authz status mismatch: Expected HTTP 403 Forbidden for invalid credentials, but API returned HTTP 400 ('{api_msg_clean}') before auth validation."
                sug_fix = "Ensure authentication/authorization middleware executes and returns HTTP 403 before request validation or business logic."
            elif actual_code == 400 and expected_code == 422:
                rc_summary = f"Validation status mismatch: Expected HTTP 422 Unprocessable Entity for invalid input, but API returned custom HTTP 400 ('{api_msg_clean}')."
                sug_fix = "Align validation error handlers to return HTTP 422 per OpenAPI 3.0 standards, or update OpenAPI spec to document HTTP 400."
            elif actual_code == 404:
                rc_summary = f"Endpoint or entity not found: The API returned HTTP 404 for '{tc.get('path', 'request')}'."
                sug_fix = "Verify the route path, path parameters, and ensure dependent resource was not deleted prior to this request."
            else:
                rc_summary = f"Status code mismatch: Expected HTTP {expected_code}, received HTTP {actual_code} with response: '{api_msg_clean or current_failure_reason}'."
                sug_fix = f"Review backend handler implementation for {tc.get('endpoint_operation_id', 'endpoint')} to return expected status code."

            normalized_sev = normalize_severity(
                proposed_severity="medium",
                category=tc.get("category", ""),
                actual_status_code=actual_code,
                expected_status_code=expected_code
            )

            finding: Finding = {
                "id": finding_id,
                "test_case_id": tc_id,
                "severity": normalized_sev,
                "confidence": 0.85,
                "root_cause_summary": rc_summary,
                "suggested_fix": sug_fix,
                "supporting_evidence": [f"HTTP {actual_code}: {api_msg_clean}"] if api_msg_clean else [],
                "is_duplicate": False,
                "guardrail_status": "pending",
                "diagnosed_status_code": actual_code,
                "diagnosed_failure_reason": current_failure_reason
            }

        findings.append(finding)
        logger.info(f"[RCA_AGENT] Successfully created diagnosis finding '{finding_id}' for '{tc_id}' (confidence: {finding['confidence']}).")

    return {
        "findings": findings,
        "status": "running"
    }
