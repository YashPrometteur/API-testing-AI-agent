import operator
from typing import Annotated, Any, Dict, List, Literal, Optional, TypedDict

TestCaseCategory = Literal[
    "functional",
    "negative",
    "edge_case",
    "security",
    "auth",
    "authz",
    "regression",
    "performance"
]

Priority = Literal["low", "medium", "high", "critical"]
ExecutionStatus = Literal["pass", "fail", "error", "flaky", "not_executed"]
Severity = Literal["low", "medium", "high", "critical"]
GuardrailStatus = Literal["pending", "auto_approved", "pending_review", "approved", "rejected"]
TestRunStatus = Literal["running", "awaiting_human", "completed", "failed"]


class EndpointSpec(TypedDict):
    path: str
    method: str
    operation_id: str
    parameters: List[Dict[str, Any]]
    request_schema: Optional[Dict[str, Any]]
    response_schemas: Dict[str, Dict[str, Any]]
    security_requirements: List[Dict[str, Any]]
    tags: List[str]


class TestCase(TypedDict):
    id: str
    endpoint_operation_id: str
    category: TestCaseCategory
    name: str
    request: Dict[str, Any]
    expected: Dict[str, Any]
    priority: Priority
    generated_by: str
    rationale: str


class ExecutionResult(TypedDict, total=False):
    test_case_id: str
    status: ExecutionStatus
    actual_status_code: Optional[int]
    actual_body: Optional[Any]
    actual_response: Optional[Any]
    latency_ms: float
    attempt_count: int
    schema_valid: Optional[bool]
    assertion_failures: List[str]
    executed_at: str
    history: List[Dict[str, Any]]


class Finding(TypedDict, total=False):
    id: str
    test_case_id: str
    severity: Severity
    confidence: float
    root_cause_summary: str
    suggested_fix: str
    supporting_evidence: List[str]
    is_duplicate: bool
    guardrail_status: GuardrailStatus
    diagnosed_status_code: Optional[int]
    diagnosed_failure_reason: Optional[str]


def merge_retry_counts(left: Dict[str, int], right: Dict[str, int]) -> Dict[str, int]:
    """Reducer function to merge retry counts from parallel node executions."""
    merged = dict(left or {})
    for k, v in (right or {}).items():
        merged[k] = max(merged.get(k, 0), v)
    return merged


def merge_execution_results(left: List[ExecutionResult], right: List[ExecutionResult]) -> List[ExecutionResult]:
    """Reducer function to merge execution results by test_case_id, maintaining accurate attempt history."""
    merged = {res["test_case_id"]: dict(res) for res in (left or [])}
    for res in (right or []):
        tc_id = res["test_case_id"]
        if tc_id in merged:
            old_res = merged[tc_id]
            old_status = old_res.get("status")
            new_status = res.get("status")
            old_history = list(old_res.get("history") or [])
            if not old_history and old_status and old_status not in ("pending_validation", "unknown"):
                old_history = [{
                    "attempt": 1,
                    "status": str(old_status).upper(),
                    "status_code": old_res.get("actual_status_code"),
                    "latency_ms": float(old_res.get("latency_ms", 0.0)),
                    "assertion_failures": list(old_res.get("assertion_failures") or []),
                    "executed_at": old_res.get("executed_at") or ""
                }]

            # If incoming result already provides its own validated history (e.g. from reflection node)
            if res.get("history"):
                merged[tc_id] = {**old_res, **res}
                continue

            # If new_status is pending_validation (raw execution started in execute_batch_node)
            if new_status == "pending_validation":
                preserved_status = old_status if old_status != "pending_validation" else "pending_validation"
                merged[tc_id] = {
                    **old_res,
                    **{k: v for k, v in res.items() if k not in ("status", "history", "attempt_count")},
                    "status": preserved_status,
                    "attempt_count": max(len(old_history) + 1, old_res.get("attempt_count", 1) + 1),
                    "history": old_history
                }
            # If this is validation output (status is pass / fail / error / not_executed)
            else:
                validated_status = (new_status or "unknown").upper()
                attempt_entry = {
                    "attempt": len(old_history) + 1,
                    "status": validated_status,
                    "status_code": res.get("actual_status_code"),
                    "latency_ms": float(res.get("latency_ms", 0.0)),
                    "assertion_failures": list(res.get("assertion_failures") or []),
                    "executed_at": res.get("executed_at") or ""
                }
                new_history = old_history + [attempt_entry]

                # Determine overall status: flaky ONLY if >= 2 attempts with differing outcomes
                has_pass = any(h["status"] == "PASS" for h in new_history)
                has_fail = any(h["status"] in ("FAIL", "ERROR") for h in new_history)

                if len(new_history) >= 2 and has_pass and has_fail:
                    overall_status = "flaky"
                elif has_pass and not has_fail:
                    overall_status = "pass"
                elif has_fail and not has_pass:
                    overall_status = new_status if new_status in ("fail", "error") else "fail"
                else:
                    overall_status = new_status

                merged[tc_id] = {
                    **old_res,
                    **res,
                    "status": overall_status,
                    "attempt_count": len(new_history),
                    "history": new_history
                }
        else:
            new_res = dict(res)
            raw_status = new_res.get("status")
            if raw_status and raw_status not in ("pending_validation", "unknown"):
                if not new_res.get("history"):
                    new_res["history"] = [{
                        "attempt": 1,
                        "status": str(raw_status).upper(),
                        "status_code": new_res.get("actual_status_code"),
                        "latency_ms": float(new_res.get("latency_ms", 0.0)),
                        "assertion_failures": list(new_res.get("assertion_failures") or []),
                        "executed_at": new_res.get("executed_at") or ""
                    }]
                new_res["attempt_count"] = len(new_res["history"])
            else:
                if not new_res.get("history"):
                    new_res["history"] = []
                new_res["attempt_count"] = 1
            merged[tc_id] = new_res

    return list(merged.values())


def merge_findings(left: List[Finding], right: List[Finding]) -> List[Finding]:
    """Reducer function to merge findings by id, updating existing finding or appending new finding."""
    merged = {f["id"]: dict(f) for f in (left or []) if isinstance(f, dict) and "id" in f}
    others = [f for f in (left or []) + (right or []) if not (isinstance(f, dict) and "id" in f)]
    for f in (right or []):
        if isinstance(f, dict) and "id" in f:
            fid = f["id"]
            if fid in merged:
                merged[fid] = {**merged[fid], **f}
            else:
                merged[fid] = f
    return list(merged.values()) + others


class TestRunState(TypedDict, total=False):
    run_id: str
    spec_id: str
    spec_source: Dict[str, Any]
    categories: List[str]
    target_operations: List[str]
    target_base_url: str
    auth_config: Dict[str, Any]
    options: Dict[str, Any]
    endpoints: List[EndpointSpec]
    test_plan: Dict[str, Any]
    test_cases: Annotated[List[TestCase], operator.add]
    execution_results: Annotated[List[ExecutionResult], merge_execution_results]
    findings: Annotated[List[Finding], merge_findings]
    reflection_count: int
    retry_counts: Annotated[Dict[str, int], merge_retry_counts]
    status: TestRunStatus
    pending_human_review: List[str]
    errors: Annotated[List[str], operator.add]
    generation_errors: Annotated[List[Dict[str, Any]], operator.add]
    final_report: Optional[Dict[str, Any]]
    execution_batches: List[Dict[str, Any]]
    route_decision: Optional[str]


