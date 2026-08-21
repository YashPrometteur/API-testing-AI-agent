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
ExecutionStatus = Literal["pass", "fail", "error", "flaky"]
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
    """Reducer function to merge execution results by test_case_id, updating in-place."""
    merged = {res["test_case_id"]: dict(res) for res in (left or [])}
    for res in (right or []):
        tc_id = res["test_case_id"]
        if tc_id in merged:
            old_status = merged[tc_id].get("status")
            new_status = res.get("status")
            old_attempts = merged[tc_id].get("attempt_count", 1)

            # If new status is pending_validation (raw re-execution from execute_batch_node),
            # increment attempt count and preserve existing validated status
            if new_status == "pending_validation":
                preserved_status = old_status if old_status != "pending_validation" else "pending_validation"
                merged[tc_id] = {
                    **merged[tc_id],
                    **{k: v for k, v in res.items() if k not in ("status", "attempt_count")},
                    "status": preserved_status,
                    "attempt_count": old_attempts + 1
                }
            # If old status was pending_validation (validation step of current attempt), preserve computed attempt count
            elif old_status == "pending_validation":
                merged[tc_id] = {
                    **merged[tc_id],
                    **res,
                    "attempt_count": max(old_attempts, res.get("attempt_count", 1))
                }
            # If status flipped between validated states (fail/error/flaky <-> pass) across attempts, mark it flaky
            elif (old_status in ("fail", "error", "flaky") and new_status == "pass") or \
                 (old_status == "pass" and new_status in ("fail", "error")):
                merged[tc_id] = {
                    **merged[tc_id],
                    **res,
                    "status": "flaky",
                    "attempt_count": max(old_attempts, res.get("attempt_count", 1))
                }
            else:
                merged[tc_id] = {
                    **merged[tc_id],
                    **res,
                    "attempt_count": max(old_attempts, res.get("attempt_count", 1))
                }
        else:
            merged[tc_id] = res
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
    final_report: Optional[Dict[str, Any]]
    execution_batches: List[Dict[str, Any]]
    route_decision: Optional[str]


