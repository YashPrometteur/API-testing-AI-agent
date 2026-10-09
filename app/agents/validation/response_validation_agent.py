import logging
from typing import Any, Dict, List
from app.graph.state import EndpointSpec, ExecutionResult, TestCase, TestRunState
from app.tools.assertion_engine import AssertionEngineTool
from app.tools.schema_validator import SchemaValidatorTool

logger = logging.getLogger("response_validation_agent")


async def response_validation_node(state: TestRunState) -> Dict[str, Any]:
    """Response Validation Agent Node (Deterministic, no LLM call).

    Validates every ExecutionResult against actual response OpenAPI schema and assertion rules.
    """
    execution_results: List[ExecutionResult] = state.get("execution_results", [])
    test_cases: List[TestCase] = state.get("test_cases", [])
    endpoints: List[EndpointSpec] = state.get("endpoints", [])

    logger.info(f"[RESPONSE_VALIDATION] Validating {len(execution_results)} execution results...")

    # Map test_cases and endpoints by ID for O(1) lookup
    tc_map: Dict[str, TestCase] = {tc["id"]: tc for tc in test_cases}
    ep_map: Dict[str, EndpointSpec] = {ep.get("operation_id", ""): ep for ep in endpoints}

    validated_results: List[ExecutionResult] = []

    for res in execution_results:
        tc_id = res["test_case_id"]
        tc = tc_map.get(tc_id)

        # If already marked error or not_executed (e.g. circuit breaker open)
        if res.get("status") in ("error", "not_executed") and res.get("actual_status_code") is None:
            validated_results.append(res)
            continue

        actual_status = res.get("actual_status_code")
        actual_body = res.get("actual_body")
        actual_headers = res.get("actual_headers", {})
        latency_ms = res.get("latency_ms", 0.0)

        # 1. Lookup response schema for the ACTUAL status code returned
        response_schema = None
        if tc and actual_status is not None:
            op_id = tc.get("endpoint_operation_id")
            ep = ep_map.get(op_id)
            if ep and "response_schemas" in ep:
                status_str = str(actual_status)
                response_schemas = ep.get("response_schemas", {})
                # Try exact status code match, e.g. "200" or "400", fallback to "default"
                if status_str in response_schemas:
                    response_schema = response_schemas[status_str]
                elif "default" in response_schemas:
                    response_schema = response_schemas["default"]

        # 2. Run SchemaValidatorTool against response schema for ACTUAL status code
        schema_res = SchemaValidatorTool.validate(actual_body, response_schema)
        schema_valid = schema_res.data.get("valid", True)
        schema_errors = schema_res.data.get("errors", [])

        # 3. Run AssertionEngineTool
        expected = tc.get("expected", {}) if tc else {}
        request = tc.get("request", {}) if tc else {}

        assertion_res = AssertionEngineTool.evaluate(
            actual_status_code=actual_status,
            actual_headers=actual_headers,
            actual_body=actual_body,
            latency_ms=latency_ms,
            expected=expected,
            schema_valid=schema_valid,
            request=request
        )

        passed = assertion_res.data.get("passed", False)
        assertion_failures = assertion_res.data.get("assertion_failures", [])
        if schema_errors and not schema_valid:
            assertion_failures.extend(schema_errors)

        # Classify final execution result status: "pass", "fail", "error"
        if actual_status is None:
            final_status = "error"
        elif passed:
            final_status = "pass"
        else:
            final_status = "fail"

        updated_res: ExecutionResult = {
            "test_case_id": tc_id,
            "status": final_status,
            "actual_status_code": actual_status,
            "actual_body": actual_body,
            "actual_response": res.get("actual_response") or actual_body,
            "latency_ms": latency_ms,
            "attempt_count": res.get("attempt_count", 1),
            "schema_valid": schema_valid,
            "assertion_failures": assertion_failures,
            "executed_at": res.get("executed_at", "")
        }
        validated_results.append(updated_res)

    pass_count = sum(1 for r in validated_results if r["status"] == "pass")
    fail_count = sum(1 for r in validated_results if r["status"] == "fail")
    error_count = sum(1 for r in validated_results if r["status"] == "error")

    logger.info(
        f"[RESPONSE_VALIDATION] Validation complete: {pass_count} Passed, {fail_count} Failed, {error_count} Errors."
    )

    return {
        "execution_results": validated_results,
        "status": "running"
    }
