import logging
from typing import Any, Dict, List, Optional
from app.tools import ToolResult

logger = logging.getLogger("assertion_engine")


class AssertionEngineTool:
    """Tool for evaluating HTTP response assertions against test case expectations."""

    @staticmethod
    def evaluate(
        actual_status_code: Optional[int],
        actual_headers: Dict[str, str],
        actual_body: Any,
        latency_ms: float,
        expected: Dict[str, Any],
        schema_valid: bool,
        request: Optional[Dict[str, Any]] = None
    ) -> ToolResult:
        """Evaluate status code, schema match, headers, latency, and field assertions."""
        assertion_failures: List[str] = []

        expected_status = expected.get("status_code")
        expected_schema_match = expected.get("response_schema_match", True)
        max_latency_ms = expected.get("max_latency_ms")
        expected_headers = expected.get("headers", {})

        # 1. Status code assertion
        if actual_status_code is None:
            assertion_failures.append(f"HTTP request failed to execute (no status code received)")
        elif expected_status is not None and actual_status_code != expected_status:
            assertion_failures.append(
                f"Status code mismatch: expected {expected_status}, got {actual_status_code}"
            )

        # 2. Response schema match assertion
        if expected_schema_match and not schema_valid:
            assertion_failures.append("Response body failed OpenAPI schema validation")

        # 3. Latency assertion (if specified)
        if max_latency_ms is not None and latency_ms > max_latency_ms:
            assertion_failures.append(
                f"Latency threshold exceeded: expected <= {max_latency_ms}ms, got {latency_ms:.2f}ms"
            )

        # 4. Header assertions (if specified)
        for h_key, h_val in expected_headers.items():
            actual_val = actual_headers.get(h_key) or actual_headers.get(h_key.lower())
            if actual_val is None:
                assertion_failures.append(f"Expected header '{h_key}' missing from response")
            elif h_val != "*" and actual_val != h_val:
                assertion_failures.append(
                    f"Header '{h_key}' mismatch: expected '{h_val}', got '{actual_val}'"
                )

        # 5. Request-to-response field reference equality assertions (if request & body exist)
        field_matches = expected.get("field_matches", {})
        if field_matches and isinstance(actual_body, dict) and request:
            req_body = request.get("body", {}) if isinstance(request.get("body"), dict) else {}
            for resp_field, req_field in field_matches.items():
                if req_field in req_body and resp_field in actual_body:
                    if req_body[req_field] != actual_body[resp_field]:
                        assertion_failures.append(
                            f"Field match failure: response['{resp_field}'] ({actual_body[resp_field]}) "
                            f"!= request['{req_field}'] ({req_body[req_field]})"
                        )

        passed = len(assertion_failures) == 0

        return ToolResult(
            success=True,
            data={
                "passed": passed,
                "assertion_failures": assertion_failures
            }
        )
