import datetime
import logging
from typing import Any, Dict, List, Optional

from app.graph.state import ExecutionResult, TestCase
from app.middleware.logging_middleware import ctx_run_id
from app.tools.circuit_breaker import HostCircuitBreaker
from app.tools.http_executor import HTTPExecutionTool

logger = logging.getLogger("execution_agents")

# Shared circuit breaker instance per process run
global_circuit_breaker = HostCircuitBreaker(max_failures=5)


async def execute_batch_node(
    payload: Dict[str, Any],
    base_url_override: Optional[str] = None,
    circuit_breaker_override: Optional[HostCircuitBreaker] = None
) -> Dict[str, Any]:
    """Batch Execution Node invoked in parallel via LangGraph Send().

    Fires HTTPExecutionTool for test cases in batch, enforcing circuit breaker checks.
    """
    batch_id = payload.get("batch_id", "batch_1")
    test_cases: List[TestCase] = payload.get("test_cases", [])
    target_base_url = base_url_override or payload.get("target_base_url", "https://petstore.swagger.io/v2")

    # Propagate run_id into contextvar — Send()-spawned tasks don't inherit
    # the parent's asyncio context, so ctx_run_id would be "" without this.
    run_id = payload.get("run_id", "")
    if run_id:
        ctx_run_id.set(run_id)

    cb = circuit_breaker_override or global_circuit_breaker
    logger.info(f"[EXECUTE_BATCH:{batch_id}] Executing batch of {len(test_cases)} test cases against '{target_base_url}'...")

    batch_results: List[ExecutionResult] = []
    retry_counts_update: Dict[str, int] = {}

    for tc in test_cases:
        tc_id = tc["id"]
        req = tc.get("request", {})

        method = req.get("method", "GET")
        rel_path = req.get("path", "")
        # Resolve full target URL
        if rel_path.startswith("http://") or rel_path.startswith("https://"):
            full_url = rel_path
        else:
            full_url = f"{target_base_url.rstrip('/')}/{rel_path.lstrip('/')}"

        # Resolve path parameters if present e.g. /pet/{petId} -> /pet/123
        path_params = req.get("path_params", {})
        for pk, pv in path_params.items():
            full_url = full_url.replace(f"{{{pk}}}", str(pv))

        # Check circuit breaker before executing
        if cb.is_open(full_url):
            logger.warning(f"[EXECUTE_BATCH:{batch_id}] Circuit breaker open for host in '{full_url}'. Skipping HTTP call for test case '{tc_id}'.")
            res: ExecutionResult = {
                "test_case_id": tc_id,
                "status": "error",
                "actual_status_code": None,
                "actual_body": {"error": "Circuit breaker open for host"},
                "latency_ms": 0.0,
                "attempt_count": 0,
                "schema_valid": False,
                "assertion_failures": [f"Circuit breaker open for host '{cb.extract_host(full_url)}'"],
                "executed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }
            batch_results.append(res)
            retry_counts_update[tc_id] = 0
            continue

        # Execute HTTP request via HTTPExecutionTool
        expected_status = tc.get("expected", {}).get("status_code")
        tool_res = await HTTPExecutionTool.execute(
            method=method,
            url=full_url,
            headers=req.get("headers"),
            query_params=req.get("query_params"),
            body=req.get("body"),
            expected_status_code=expected_status,
            max_retries=3
        )

        res_data = tool_res.data or {}
        attempts = res_data.get("attempt_count", 1)
        transient = res_data.get("transient_error", False)
        status_code = res_data.get("status_code")

        retry_counts_update[tc_id] = attempts

        if transient:
            cb.record_transient_failure(full_url)
        else:
            cb.record_success(full_url)

        # Initial raw execution result (response validation agent will populate schema_valid, assertion_failures, and status)
        initial_status = "error" if not tool_res.success and transient else "pending_validation"

        raw_res: ExecutionResult = {
            "test_case_id": tc_id,
            "status": initial_status,
            "actual_status_code": status_code,
            "actual_body": res_data.get("body"),
            "actual_response": res_data.get("actual_response") or res_data.get("body"),
            "latency_ms": res_data.get("latency_ms", 0.0),
            "attempt_count": attempts,
            "schema_valid": None,
            "assertion_failures": [],
            "executed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        # Attach actual headers in raw_res dict for assertion engine evaluation
        raw_res["actual_headers"] = res_data.get("headers", {})
        batch_results.append(raw_res)

    logger.info(f"[EXECUTE_BATCH:{batch_id}] Completed execution for {len(batch_results)} test cases.")

    return {
        "execution_results": batch_results,
        "retry_counts": retry_counts_update
    }
