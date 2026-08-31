import datetime
import logging
import re
from typing import Any, Dict, List, Optional

from app.graph.state import ExecutionResult, TestCase
from app.middleware.logging_middleware import ctx_run_id
from app.tools.circuit_breaker import HostCircuitBreaker
from app.tools.http_executor import HTTPExecutionTool

logger = logging.getLogger("execution_agents")

# Shared circuit breaker instance per process run
global_circuit_breaker = HostCircuitBreaker(max_failures=5)

# Process-level registry to share discovered IDs across parallel execution batches per run
RUN_DISCOVERED_IDS: Dict[str, Dict[str, str]] = {}


def extract_ids_from_response(body: Any) -> Dict[str, str]:
    """Recursively search response body for primary identifier strings (_id, id, plan_id, etc.)."""
    extracted: Dict[str, str] = {}
    if not body:
        return extracted

    def _search(obj: Any, parent_key: str = ""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_lower = k.lower()
                if (k_lower in ("_id", "id") or k_lower.endswith("_id") or k_lower.endswith("id")) and isinstance(v, (str, int)):
                    v_str = str(v).strip()
                    if v_str and v_str != "None":
                        extracted[k_lower] = v_str
                        if parent_key:
                            extracted[f"{parent_key}_id"] = v_str
                        if k_lower in ("_id", "id"):
                            extracted["primary_id"] = v_str
                if isinstance(v, (dict, list)):
                    _search(v, parent_key=k_lower)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    _search(item, parent_key=parent_key)

    _search(body)
    return extracted


async def execute_batch_node(
    payload: Dict[str, Any],
    base_url_override: Optional[str] = None,
    circuit_breaker_override: Optional[HostCircuitBreaker] = None
) -> Dict[str, Any]:
    """Batch Execution Node invoked in parallel via LangGraph Send().

    Fires HTTPExecutionTool for test cases in batch, enforcing circuit breaker checks
    and dynamic ID chaining across dependent endpoints.
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
    auth_config = payload.get("auth_config") or {}
    logger.info(f"[EXECUTE_BATCH:{batch_id}] Executing batch of {len(test_cases)} test cases against '{target_base_url}'...")

    batch_results: List[ExecutionResult] = []
    retry_counts_update: Dict[str, int] = {}

    # Initialize shared ID context for this run
    if run_id not in RUN_DISCOVERED_IDS:
        RUN_DISCOVERED_IDS[run_id] = {}

    dynamic_context_ids = RUN_DISCOVERED_IDS[run_id]

    for tc in test_cases:
        tc_id = tc["id"]
        req = tc.get("request", {})
        cat = str(tc.get("category", "")).lower()
        tc_name = str(tc.get("name", "")).lower()

        method = req.get("method", "GET")
        rel_path = req.get("path", "")
        # Resolve full target URL
        if rel_path.startswith("http://") or rel_path.startswith("https://"):
            full_url = rel_path
        else:
            full_url = f"{target_base_url.rstrip('/')}/{rel_path.lstrip('/')}"

        path_params = dict(req.get("path_params") or {})
        query_params = dict(req.get("query_params") or {})

        # Determine if this is specifically an intentional negative ID test (e.g. testing malformed ID format)
        is_negative_id_test = (
            "invalid plan_id" in tc_name
            or "invalid plan id" in tc_name
            or "invalid id" in tc_name
            or "invalid_id" in tc_name
            or "invalid_plan_id" in rel_path.lower()
            or "nonexistent" in tc_name
            or "nonexistent" in rel_path.lower()
        )

        # Dynamic ID substitution: Replace dummy and mock placeholder IDs with live created IDs
        if not is_negative_id_test and dynamic_context_ids:
            # 1. Update path_params with real live discovered ID
            for pk in list(path_params.keys()):
                pk_clean = pk.lower().strip("{}")
                matched_id = (
                    dynamic_context_ids.get(pk_clean)
                    or dynamic_context_ids.get(f"{pk_clean}_id")
                    or dynamic_context_ids.get("primary_id")
                    or dynamic_context_ids.get("_id")
                    or dynamic_context_ids.get("id")
                )
                if matched_id:
                    path_params[pk] = matched_id

            # 2. Update query_params if parameter is an identifier and not empty
            for qk in list(query_params.keys()):
                qk_clean = qk.lower()
                if (qk_clean in ("plan_id", "id", "subscription_id") or qk_clean.endswith("_id")) and query_params[qk]:
                    matched_id = dynamic_context_ids.get(qk_clean) or dynamic_context_ids.get("primary_id") or dynamic_context_ids.get("_id")
                    if matched_id:
                        query_params[qk] = matched_id

            # 3. Update direct path dummy URL segments and mock fallback IDs (e.g. /64b8f0e9e5162e84ea981e01 -> /<real_id>)
            matched_real_id = dynamic_context_ids.get("plan_id") or dynamic_context_ids.get("primary_id") or dynamic_context_ids.get("_id") or dynamic_context_ids.get("id")
            if matched_real_id:
                full_url = re.sub(r"/64b8f0e9e5162e84ea981e[0-9a-fA-F]{2}", f"/{matched_real_id}", full_url)
                for dummy_val in ("123", "12345", "valid_plan_id", "valid_id", "some_plan_id", "task_001"):
                    if f"/{dummy_val}" in full_url:
                        full_url = full_url.replace(f"/{dummy_val}", f"/{matched_real_id}")

        # Resolve path parameters if present e.g. /pet/{petId} -> /pet/123
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
                "executed_at": datetime.timezone.utc and datetime.datetime.now(datetime.timezone.utc).isoformat()
            }
            batch_results.append(res)
            retry_counts_update[tc_id] = 0
            continue

        # Prepare headers and inject auth_config if provided
        headers = dict(req.get("headers") or {})
        if auth_config:
            token = auth_config.get("token") or auth_config.get("key")
            auth_type = str(auth_config.get("type") or "bearer").lower()
            has_auth = any(k.lower() == "authorization" for k in headers.keys())
            if not (cat in ("auth", "authz") and has_auth):
                if token:
                    if auth_type == "bearer":
                        headers["Authorization"] = token if str(token).startswith("Bearer ") else f"Bearer {token}"
                    elif auth_type == "api_key":
                        key_name = auth_config.get("name", "X-API-Key")
                        headers[key_name] = str(token)
                    elif auth_type == "basic":
                        headers["Authorization"] = str(token) if str(token).startswith("Basic ") else f"Basic {token}"

        # Execute HTTP request via HTTPExecutionTool
        expected_status = tc.get("expected", {}).get("status_code")
        tool_res = await HTTPExecutionTool.execute(
            method=method,
            url=full_url,
            headers=headers,
            query_params=query_params,
            body=req.get("body"),
            expected_status_code=expected_status,
            max_retries=3
        )

        res_data = tool_res.data or {}
        attempts = res_data.get("attempt_count", 1)
        transient = res_data.get("transient_error", False)
        status_code = res_data.get("status_code")

        # Capture dynamic IDs from successful responses (e.g. GET list or POST create)
        if status_code in (200, 201) and res_data.get("body"):
            new_ids = extract_ids_from_response(res_data.get("body"))
            if new_ids:
                dynamic_context_ids.update(new_ids)
                if run_id:
                    RUN_DISCOVERED_IDS[run_id].update(new_ids)
                logger.info(f"[EXECUTE_BATCH:{batch_id}] Captured dynamic IDs for run '{run_id}': {new_ids}")

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
