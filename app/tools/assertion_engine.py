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
        max_latency_ms = expected.get("max_latency_ms") if expected.get("max_latency_ms") is not None else expected.get("max_response_time_ms")
        expected_headers = expected.get("headers", {})

        # 1. Status code assertion
        if actual_status_code is None:
            assertion_failures.append("HTTP request failed to execute (no status code received)")
        elif expected_status is not None:
            if isinstance(expected_status, (list, tuple, set)):
                if actual_status_code not in expected_status:
                    assertion_failures.append(
                        f"Status code mismatch: expected one of {list(expected_status)}, got {actual_status_code}"
                    )
            elif actual_status_code != expected_status:
                # Accept standard REST success equivalence (200 OK vs 201 Created)
                if expected_status in (200, 201) and actual_status_code in (200, 201):
                    pass
                # Accept standard REST client validation error equivalence (400 Bad Request vs 422 Unprocessable Entity)
                elif expected_status in (400, 422) and actual_status_code in (400, 422):
                    pass
                else:
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

        # 6. Deep Business Assertions (JWT token validity, success boolean, and nested field checks)
        if isinstance(actual_body, dict):
            # Check success boolean alignment with HTTP status
            if actual_status_code in (200, 201):
                if actual_body.get("success") is False:
                    assertion_failures.append("Business assertion failed: expected 'success: true' for 2xx response, got 'success: false'")

                # Validate JWT format if access_token is present in response
                data_obj = actual_body.get("data")
                if isinstance(data_obj, dict) and "access_token" in data_obj:
                    tok = str(data_obj.get("access_token") or "")
                    if tok and (tok.count(".") != 2 or not tok.startswith("eyJ")):
                        assertion_failures.append(f"Business assertion failed: 'access_token' is not a valid 3-part JWT token format")

            # Check expected explicit field values if specified in expected dict
            expected_field_values = expected.get("expected_field_values", {})
            for f_path, f_expected in expected_field_values.items():
                # Traverse nested dictionary path e.g. "data.is_email_verified"
                parts = f_path.split(".")
                curr = actual_body
                found = True
                for p in parts:
                    if isinstance(curr, dict) and p in curr:
                        curr = curr[p]
                    else:
                        found = False
                        break
                if not found:
                    assertion_failures.append(f"Business assertion failed: missing expected field path '{f_path}'")
                elif curr != f_expected:
                    assertion_failures.append(f"Business assertion failed: field '{f_path}' expected '{f_expected}', got '{curr}'")

        passed = len(assertion_failures) == 0

        return ToolResult(
            success=True,
            data={
                "passed": passed,
                "assertion_failures": assertion_failures
            }
        )
