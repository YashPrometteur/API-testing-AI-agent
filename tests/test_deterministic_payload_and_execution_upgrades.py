import pytest
import re
from unittest.mock import AsyncMock, patch

from app.graph.state import EndpointSpec, TestCase
from app.tools.payload_generator import SchemaPayloadGenerator
from app.agents.generators.base_generator import generate_category_test_cases
from app.agents.execution.execution_agents import execute_batch_node
from app.agents.reporting.reporting_agent import reporting_agent_node
from app.tools.report_renderer import ReportRendererTool


@pytest.mark.asyncio
async def test_deterministic_payload_generator_omissions_and_wrong_types():
    """Verify SchemaPayloadGenerator produces required field omissions, wrong types, edge cases, SQLi."""
    endpoint: EndpointSpec = {
        "operation_id": "create_item",
        "method": "POST",
        "path": "/items",
        "parameters": [],
        "request_schema": {
            "type": "object",
            "required": ["item_name", "price", "category_code"],
            "properties": {
                "item_name": {"type": "string", "minLength": 3, "maxLength": 50},
                "price": {"type": "number", "minimum": 0.0},
                "category_code": {"type": "string", "enum": ["ELEC", "FURN", "CLOTH"]}
            }
        },
        "response_schemas": {"201": {"type": "object"}},
        "security_requirements": [{"BearerAuth": []}],
        "tags": ["Items"]
    }

    # 1. Functional
    func_cases = SchemaPayloadGenerator.generate_functional(endpoint)
    assert len(func_cases) >= 1
    assert func_cases[0]["request"]["body"]["item_name"]
    assert func_cases[0]["expected"]["max_latency_ms"] == 5000.0

    # 2. Negative - Omissions, wrong types, enum
    neg_cases = SchemaPayloadGenerator.generate_negative(endpoint)
    # 3 required omissions + 3 wrong types + 1 invalid enum = 7 cases
    assert len(neg_cases) >= 7

    omitted_names = [c["name"] for c in neg_cases if "Missing required field" in c["name"]]
    assert any("item_name" in name for name in omitted_names)
    assert any("price" in name for name in omitted_names)
    assert any("category_code" in name for name in omitted_names)

    enum_cases = [c for c in neg_cases if "Invalid enum" in c["name"]]
    assert len(enum_cases) >= 1
    assert enum_cases[0]["request"]["body"]["category_code"] == "INVALID_ENUM_VALUE_XYZ"

    # 3. Edge cases
    edge_cases = SchemaPayloadGenerator.generate_edge_case(endpoint)
    assert len(edge_cases) >= 4
    # Check negative price boundary
    assert any(c["request"]["body"]["price"] < 0 for c in edge_cases if isinstance(c["request"]["body"].get("price"), (int, float)))

    # 4. Security
    sec_cases = SchemaPayloadGenerator.generate_security(endpoint)
    assert len(sec_cases) >= 3
    assert any("SQLi" in c["name"] for c in sec_cases)


@pytest.mark.asyncio
async def test_target_case_count_enforcement_and_no_petstore_fallback():
    """Verify target_case_count is fulfilled with deterministic generator even when LLM fails."""
    endpoint: EndpointSpec = {
        "operation_id": "create_payment",
        "method": "POST",
        "path": "/payments",
        "parameters": [],
        "request_schema": {
            "type": "object",
            "required": ["amount", "currency"],
            "properties": {
                "amount": {"type": "number", "minimum": 1.0},
                "currency": {"type": "string", "enum": ["USD", "EUR"]}
            }
        },
        "response_schemas": {"201": {}},
        "security_requirements": [],
        "tags": []
    }

    state_payload = {
        "run_id": "run_test_counts",
        "endpoints": [endpoint],
        "test_plan": {
            "plan": {
                "create_payment": {
                    "categories": ["negative"],
                    "target_case_count": {"negative": 5},
                    "priority": "high"
                }
            }
        }
    }

    # Mock failing LLM client
    mock_failing_llm = AsyncMock()
    mock_failing_llm.generate.side_effect = RuntimeError("OpenRouter 429 Rate Limit Exceeded")

    res = await generate_category_test_cases(
        category="negative",
        prompt_filename="negative_v1.md",
        state_or_payload=state_payload,
        llm_client=mock_failing_llm
    )

    tcs = res.get("test_cases", [])
    assert len(tcs) >= 3
    # Ensure no Petstore strings leaked
    for tc in tcs:
        body_str = str(tc.get("request", {}).get("body", ""))
        assert "doggie" not in body_str
        assert "photoUrls" not in body_str
        assert "max_latency_ms" in tc.get("expected", {})


@pytest.mark.asyncio
async def test_auth_config_and_omit_auth_execution():
    """Verify auth headers are injected for normal tests and stripped when omit_auth=True."""
    auth_config = {
        "type": "bearer",
        "token": "secret_jwt_token_123",
        "default_headers": {"X-Custom-Tenant": "tenant_abc"}
    }

    normal_tc: TestCase = {
        "id": "tc_func_01",
        "endpoint_operation_id": "get_profile",
        "category": "functional",
        "name": "Get Profile",
        "request": {"method": "GET", "path": "/profile", "headers": {}},
        "expected": {"status_code": 200},
        "priority": "high",
        "generated_by": "test",
        "rationale": "test"
    }

    auth_omitted_tc: TestCase = {
        "id": "tc_auth_01",
        "endpoint_operation_id": "get_profile",
        "category": "auth",
        "name": "Get Profile - Unauthenticated",
        "request": {"method": "GET", "path": "/profile", "headers": {"Authorization": "Bearer dummy"}, "omit_auth": True},
        "expected": {"status_code": 401},
        "priority": "high",
        "generated_by": "test",
        "rationale": "test"
    }

    with patch("app.tools.http_executor.HTTPExecutionTool.execute", new_callable=AsyncMock) as mock_http:
        from app.tools import ToolResult
        mock_http.return_value = ToolResult(success=True, data={"status_code": 200, "body": {"id": "123"}})

        await execute_batch_node(
            payload={
                "batch_id": "b1",
                "run_id": "run_auth_check",
                "test_cases": [normal_tc, auth_omitted_tc],
                "target_base_url": "https://api.example.com",
                "auth_config": auth_config
            }
        )

        assert mock_http.call_count == 2
        # First call (normal functional test) must include Authorization and X-Custom-Tenant
        first_call_headers = mock_http.call_args_list[0].kwargs["headers"]
        assert first_call_headers["Authorization"] == "Bearer secret_jwt_token_123"
        assert first_call_headers["X-Custom-Tenant"] == "tenant_abc"

        # Second call (auth category with omit_auth=True) must have stripped Authorization
        second_call_headers = mock_http.call_args_list[1].kwargs["headers"]
        assert "Authorization" not in second_call_headers
        assert second_call_headers["X-Custom-Tenant"] == "tenant_abc"


@pytest.mark.asyncio
async def test_unresolved_path_parameter_preflight_rejection():
    """Verify test cases with unresolved {param} are failed pre-flight and not sent over the wire."""
    tc_unresolved: TestCase = {
        "id": "tc_path_fail_01",
        "endpoint_operation_id": "get_org_order",
        "category": "functional",
        "name": "Get Order with Unknown Placeholder",
        "request": {"method": "GET", "path": "/orgs/{unknown_org_id}/orders/{unknown_order_id}", "headers": {}, "path_params": {}},
        "expected": {"status_code": 200},
        "priority": "high",
        "generated_by": "test",
        "rationale": "test"
    }

    with patch("app.tools.http_executor.HTTPExecutionTool.execute", new_callable=AsyncMock) as mock_http:
        res = await execute_batch_node(
            payload={
                "batch_id": "b1",
                "run_id": "run_unresolved_path",
                "test_cases": [tc_unresolved],
                "target_base_url": "https://api.example.com"
            }
        )

        assert mock_http.call_count == 0  # Pre-flight blocked HTTP call
        exec_results = res.get("execution_results", [])
        assert len(exec_results) == 1
        assert exec_results[0]["status"] == "error"
        assert any("Pre-flight path validation failed" in f for f in exec_results[0]["assertion_failures"])


@pytest.mark.asyncio
async def test_post_create_to_get_id_chaining():
    """Verify POST create dynamically chains real created resource ID to downstream GET requests."""
    tc_create: TestCase = {
        "id": "tc_create_01",
        "endpoint_operation_id": "create_pet",
        "category": "functional",
        "name": "Create Pet",
        "request": {"method": "POST", "path": "/pets", "headers": {}, "body": {"name": "Charlie"}},
        "expected": {"status_code": 201},
        "priority": "high",
        "generated_by": "test",
        "rationale": "test"
    }

    tc_get: TestCase = {
        "id": "tc_get_01",
        "endpoint_operation_id": "get_pet_by_id",
        "category": "functional",
        "name": "Get Pet By ID",
        "request": {"method": "GET", "path": "/pets/{pet_id}", "headers": {}, "path_params": {"pet_id": "placeholder"}},
        "expected": {"status_code": 200},
        "priority": "high",
        "generated_by": "test",
        "rationale": "test"
    }

    with patch("app.tools.http_executor.HTTPExecutionTool.execute", new_callable=AsyncMock) as mock_http:
        from app.tools import ToolResult
        # POST returns created id "real_pet_999"
        mock_http.side_effect = [
            ToolResult(success=True, data={"status_code": 201, "body": {"id": "real_pet_999", "name": "Charlie"}}),
            ToolResult(success=True, data={"status_code": 200, "body": {"id": "real_pet_999", "name": "Charlie"}})
        ]

        await execute_batch_node(
            payload={
                "batch_id": "b1",
                "run_id": "run_id_chain_demo",
                "test_cases": [tc_create, tc_get],
                "target_base_url": "https://api.example.com"
            }
        )

        assert mock_http.call_count == 2
        # Second call URL should have substituted "real_pet_999"
        second_call_url = mock_http.call_args_list[1].kwargs["url"]
        assert "real_pet_999" in second_call_url


@pytest.mark.asyncio
async def test_body_session_id_and_otp_chaining():
    """Verify request-otp response session_id and OTP are dynamically chained into verify-otp and register body payloads."""
    tc_req_otp: TestCase = {
        "id": "tc_req_01",
        "endpoint_operation_id": "request_otp",
        "category": "functional",
        "name": "Request OTP",
        "request": {"method": "POST", "path": "/api/users/request-otp", "body": {"identifier": "qa@example.com", "type": "email"}},
        "expected": {"status_code": 200},
        "priority": "high",
        "generated_by": "test",
        "rationale": "test"
    }

    tc_verify_otp: TestCase = {
        "id": "tc_ver_01",
        "endpoint_operation_id": "verify_otp",
        "category": "functional",
        "name": "Verify OTP",
        "request": {"method": "POST", "path": "/api/users/verify-otp", "body": {"session_id": "550e8400-e29b-41d4-a716-446655440000", "otp": "123456"}},
        "expected": {"status_code": 200},
        "priority": "high",
        "generated_by": "test",
        "rationale": "test"
    }

    tc_register: TestCase = {
        "id": "tc_reg_01",
        "endpoint_operation_id": "register_user",
        "category": "functional",
        "name": "Register User",
        "request": {"method": "POST", "path": "/api/users/register", "body": {"first_name": "Alexander", "last_name": "Johnson", "session_id": "550e8400-e29b-41d4-a716-446655440000"}},
        "expected": {"status_code": 201},
        "priority": "high",
        "generated_by": "test",
        "rationale": "test"
    }

    with patch("app.tools.http_executor.HTTPExecutionTool.execute", new_callable=AsyncMock) as mock_http:
        from app.tools import ToolResult
        mock_http.side_effect = [
            ToolResult(success=True, data={"status_code": 200, "body": {"data": {"session_id": "real_session_abc123", "otp": "998877"}}}),
            ToolResult(success=True, data={"status_code": 200, "body": {"data": {"session_id": "real_session_abc123", "verified": True}}}),
            ToolResult(success=True, data={"status_code": 201, "body": {"data": {"user_id": "u_999"}}})
        ]

        await execute_batch_node(
            payload={
                "batch_id": "b_otp_flow",
                "run_id": "run_otp_chain_test",
                "test_cases": [tc_req_otp, tc_verify_otp, tc_register],
                "target_base_url": "https://api.example.com"
            }
        )

        assert mock_http.call_count == 3
        # 1. verify-otp should receive real_session_abc123 and 998877 in body
        verify_body = mock_http.call_args_list[1].kwargs["body"]
        assert verify_body["session_id"] == "real_session_abc123"
        assert verify_body["otp"] == "998877"

        # 2. register should receive real_session_abc123 in body
        reg_body = mock_http.call_args_list[2].kwargs["body"]
        assert reg_body["session_id"] == "real_session_abc123"



@pytest.mark.asyncio
async def test_reporting_summary_endpoints_and_html_rendering():
    """Verify reporting agent outputs summary.endpoints and HTML report contains Results by API Endpoint."""
    state = {
        "run_id": "run_report_check",
        "spec_id": "spec_123",
        "endpoints": [
            {"operation_id": "get_users", "method": "GET", "path": "/api/v1/users"}
        ],
        "test_cases": [
            {
                "id": "tc_u1",
                "endpoint_operation_id": "get_users",
                "category": "functional",
                "name": "Get Users Valid",
                "request": {"method": "GET", "path": "/api/v1/users"},
                "expected": {"status_code": 200, "max_latency_ms": 5000.0}
            }
        ],
        "execution_results": [
            {
                "test_case_id": "tc_u1",
                "status": "pass",
                "actual_status_code": 200,
                "actual_body": [{"id": "1", "name": "Alice"}],
                "actual_response": [{"id": "1", "name": "Alice"}],
                "latency_ms": 45.0,
                "attempt_count": 1,
                "schema_valid": True,
                "assertion_failures": []
            }
        ],
        "findings": []
    }

    mock_db = AsyncMock()
    mock_db["runs"].find_one.return_value = {"_id": "run_report_check", "status": "completed"}
    mock_db["defects"].find.return_value.to_list = AsyncMock(return_value=[])
    mock_db["findings"].find.return_value.to_list = AsyncMock(return_value=[])
    mock_db["execution_history"].update_one = AsyncMock()
    mock_db["reports"].update_one = AsyncMock()
    mock_llm = AsyncMock()
    from app.agents.reporting.reporting_agent import ReportSummary
    mock_llm.generate_structured.return_value = ReportSummary(summary_text="Execution completed successfully with all mock checks passed.")

    with patch("app.agents.reporting.reporting_agent.get_llm_client", return_value=mock_llm):
        res = await reporting_agent_node(state, db_override=mock_db)
    final_report = res.get("final_report", {})
    summary = final_report.get("summary", {})

    assert "endpoints" in summary
    assert "get_users" in summary["endpoints"]
    ep_data = summary["endpoints"]["get_users"]
    assert ep_data["pass_count"] == 1
    assert ep_data["pass_rate"] == 1.0
    assert "test_case_executions" in final_report
    assert len(final_report["test_case_executions"]) == 1

    # Render HTML template
    html = ReportRendererTool.render_html(final_report)
    assert "Results by API Endpoint" in html
    assert "/api/v1/users" in html
    assert "Get Users Valid" in html


@pytest.mark.asyncio
async def test_generation_errors_attached_to_final_report_and_rendered():
    """Verify generation_errors are attached to final_report and rendered in HTML and PDF."""
    state = {
        "run_id": "run_gen_err_test",
        "spec_id": "spec_err_123",
        "endpoints": [{"operation_id": "get_orders", "method": "GET", "path": "/orders"}],
        "test_cases": [],
        "execution_results": [],
        "findings": [],
        "generation_errors": [
            {
                "operation_id": "get_orders",
                "category": "security",
                "error": "LLM generation failed (RateLimitError). Deterministic schema generator fallback used.",
                "fallback": "deterministic_schema_generator"
            }
        ]
    }

    mock_db = AsyncMock()
    mock_db["runs"].find_one.return_value = {"_id": "run_gen_err_test", "status": "completed"}
    mock_db["defects"].find.return_value.to_list = AsyncMock(return_value=[])
    mock_db["findings"].find.return_value.to_list = AsyncMock(return_value=[])
    mock_db["execution_history"].update_one = AsyncMock()
    mock_db["reports"].update_one = AsyncMock()
    mock_db["runs"].update_one = AsyncMock()
    mock_db["test_case_executions"].bulk_write = AsyncMock()

    res = await reporting_agent_node(state, db_override=mock_db)
    final_report = res.get("final_report", {})
    assert "generation_errors" in final_report
    assert len(final_report["generation_errors"]) == 1
    assert final_report["generation_errors"][0]["category"] == "security"

    # HTML rendering
    html = ReportRendererTool.render_html(final_report)
    assert "Generation Warnings" in html and "LLM Fallbacks" in html
    assert "get_orders" in html

    # PDF rendering
    pdf_bytes = ReportRendererTool.render_pdf(final_report)
    assert len(pdf_bytes) > 0


@pytest.mark.asyncio
async def test_regression_generator_reads_prior_test_cases_matching_spec_id():
    """Verify regression generator queries db['test_cases'] by spec_id and formats prior cases."""
    from app.agents.generators.regression import regression_generator_node

    mock_db = AsyncMock()
    prior_case_doc = {
        "_id": "tc_hist_99",
        "spec_id": "spec_regr_test",
        "endpoint_operation_id": "get_items",
        "category": "functional",
        "name": "Historical Item Fetch",
        "request": {"method": "GET", "path": "/items"},
        "expected": {"status_code": 200, "max_latency_ms": 5000.0},
        "execution_history": {"flake_rate": 0.3, "confirmed_defects": 1},
        "created_at": "2026-08-01T00:00:00Z"
    }

    from unittest.mock import MagicMock
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.limit.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(return_value=[prior_case_doc])
    mock_db["test_cases"].find.return_value = mock_cursor

    payload = {
        "run_id": "run_regr_check_01",
        "spec_id": "spec_regr_test",
        "endpoints": [{"operation_id": "get_items", "method": "GET", "path": "/items"}]
    }

    res = await regression_generator_node(payload, db_override=mock_db)
    assert "test_cases" in res
    assert len(res["test_cases"]) == 1
    tc = res["test_cases"][0]
    assert tc["id"] == "tc_hist_99"
    assert tc["category"] == "regression"
    assert tc["endpoint_operation_id"] == "get_items"


@pytest.mark.asyncio
async def test_bug1_stale_findings_cleared_for_passing_tests():
    """Verify that a test case which failed in earlier pass and later passed carries NO finding in report."""
    state = {
        "run_id": "run_bug1_check",
        "spec_id": "spec_bug1",
        "endpoints": [{"operation_id": "update_sub", "method": "PUT", "path": "/subscriptions/sub_123"}],
        "test_cases": [
            {
                "id": "tc_sub_01",
                "endpoint_operation_id": "update_sub",
                "category": "functional",
                "name": "Update Subscription",
                "request": {"method": "PUT", "path": "/subscriptions/sub_123"},
                "expected": {"status_code": 200, "max_latency_ms": 5000.0}
            }
        ],
        "execution_results": [
            {
                "test_case_id": "tc_sub_01",
                "status": "pass",
                "actual_status_code": 200,
                "actual_body": {"success": True},
                "actual_response": {"success": True},
                "latency_ms": 65.0,
                "attempt_count": 1,
                "schema_valid": True,
                "assertion_failures": []
            }
        ],
        # Stale finding created during reflection loop 0 when path param was unresolved
        "findings": [
            {
                "id": "find_stale_sub_01",
                "test_case_id": "tc_sub_01",
                "severity": "high",
                "confidence": 0.9,
                "root_cause_summary": "Pre-flight path validation failed: unresolved path token(s) ['plan_id']",
                "suggested_fix": "Supply plan_id parameter",
                "supporting_evidence": [],
                "guardrail_status": "auto_approved"
            }
        ]
    }

    mock_db = AsyncMock()
    mock_db["runs"].find_one.return_value = {"_id": "run_bug1_check", "status": "completed"}
    mock_db["defects"].find.return_value.to_list = AsyncMock(return_value=[])
    mock_db["findings"].find_one.return_value = None
    mock_db["execution_history"].update_one = AsyncMock()
    mock_db["reports"].update_one = AsyncMock()
    mock_db["runs"].update_one = AsyncMock()
    mock_db["test_case_executions"].bulk_write = AsyncMock()

    res = await reporting_agent_node(state, db_override=mock_db)
    final_report = res.get("final_report", {})

    # 1. Final report findings list MUST NOT contain the stale finding for the passing test
    assert len(final_report["findings"]) == 0

    # 2. Attached execution record MUST have finding = None
    execs = final_report.get("test_case_executions", [])
    assert len(execs) == 1
    assert execs[0]["execution_status"] == "PASS"
    assert execs[0]["finding"] is None


@pytest.mark.asyncio
async def test_bug2_flaky_requires_multiple_differing_attempts():
    """Verify that execution_status is only FLAKY when backed by >= 2 differing attempts in history."""
    from app.graph.state import merge_execution_results

    # 1. Single attempt passing: must be PASS, not FLAKY
    single_attempt_result = [{
        "test_case_id": "tc_single_pass",
        "status": "pass",
        "actual_status_code": 200,
        "latency_ms": 50.0,
        "assertion_failures": []
    }]
    merged_single = merge_execution_results([], single_attempt_result)
    assert merged_single[0]["status"] == "pass"
    assert merged_single[0]["attempt_count"] == 1
    assert len(merged_single[0]["history"]) == 1
    assert merged_single[0]["history"][0]["status"] == "PASS"

    # 2. Multi-attempt result: Attempt 1 FAIL, Attempt 2 PASS -> FLAKY
    attempt_1 = [{
        "test_case_id": "tc_multi_flaky",
        "status": "fail",
        "actual_status_code": 500,
        "latency_ms": 120.0,
        "assertion_failures": ["Server crash"]
    }]
    state_after_att1 = merge_execution_results([], attempt_1)
    assert state_after_att1[0]["status"] == "fail"

    attempt_2 = [{
        "test_case_id": "tc_multi_flaky",
        "status": "pass",
        "actual_status_code": 200,
        "latency_ms": 40.0,
        "assertion_failures": []
    }]
    state_after_att2 = merge_execution_results(state_after_att1, attempt_2)
    assert state_after_att2[0]["status"] == "flaky"
    assert state_after_att2[0]["attempt_count"] == 2
    assert len(state_after_att2[0]["history"]) == 2
    assert state_after_att2[0]["history"][0]["status"] == "FAIL"
    assert state_after_att2[0]["history"][1]["status"] == "PASS"


@pytest.mark.asyncio
async def test_flaky_test_with_final_pass_has_no_stale_finding():
    """Verify that a test marked FLAKY whose final attempt passed does NOT carry a finding and has fallback summary."""
    state = {
        "run_id": "run_flaky_clean",
        "spec_id": "spec_flaky_check",
        "endpoints": [{"operation_id": "patch_sub", "method": "PATCH", "path": "/subscriptions/sub_123"}],
        "test_cases": [
            {
                "id": "tc_flaky_01",
                "endpoint_operation_id": "patch_sub",
                "category": "edge_case",
                "name": "Update Plan Feature",
                "request": {"method": "PATCH", "path": "/subscriptions/sub_123"},
                "expected": {"status_code": 200, "max_latency_ms": 5000.0}
            }
        ],
        "execution_results": [
            {
                "test_case_id": "tc_flaky_01",
                "status": "flaky",
                "actual_status_code": 200,
                "actual_body": {"message": "Subscription updated successfully."},
                "actual_response": {"message": "Subscription updated successfully."},
                "latency_ms": 511.0,
                "attempt_count": 2,
                "schema_valid": True,
                "assertion_failures": [],
                "history": [
                    {
                        "attempt": 1,
                        "status": "ERROR",
                        "status_code": None,
                        "assertion_failures": ["Pre-flight path validation failed: unresolved path token(s) ['plan_id']"]
                    },
                    {
                        "attempt": 2,
                        "status": "PASS",
                        "status_code": 200,
                        "assertion_failures": []
                    }
                ]
            }
        ],
        # Finding generated during attempt 1 error
        "findings": [
            {
                "id": "find_att1_preflight",
                "test_case_id": "tc_flaky_01",
                "severity": "medium",
                "confidence": 0.85,
                "root_cause_summary": "Pre-flight path validation failed: unresolved path token(s) ['plan_id']",
                "suggested_fix": "Supply valid ID",
                "supporting_evidence": []
            }
        ]
    }

    mock_db = AsyncMock()
    mock_db["runs"].find_one.return_value = {"_id": "run_flaky_clean", "status": "completed"}
    mock_db["defects"].find.return_value.to_list = AsyncMock(return_value=[])
    mock_db["findings"].find_one.return_value = None
    mock_db["execution_history"].update_one = AsyncMock()
    mock_db["reports"].update_one = AsyncMock()
    mock_db["runs"].update_one = AsyncMock()
    mock_db["test_case_executions"].bulk_write = AsyncMock()

    res = await reporting_agent_node(state, db_override=mock_db)
    final_report = res.get("final_report", {})

    # 1. Findings list in final_report MUST NOT contain the resolved attempt 1 finding
    assert len(final_report["findings"]) == 0

    # 2. test_case_executions record MUST have execution_status = FLAKY, history with 2 attempts, and finding = None
    execs = final_report.get("test_case_executions", [])
    assert len(execs) == 1
    assert execs[0]["execution_status"] == "FLAKY"
    assert execs[0]["finding"] is None
    assert len(execs[0]["history"]) == 2

    # 3. Executive summary is populated with fallback text and not empty
    assert len(final_report["executive_summary"]) > 0


@pytest.mark.asyncio
async def test_valid_llm_response_merges_and_deduplicates_with_deterministic_cases():
    """Verify that when LLM returns a well-formed response, cases are merged, deduplicated, and attributed correctly."""
    from app.models.test_case import TestCaseLLMInputListModel, TestCaseLLMInputModel

    endpoint: EndpointSpec = {
        "operation_id": "create_item",
        "method": "POST",
        "path": "/items",
        "parameters": [],
        "request_schema": {
            "type": "object",
            "required": ["item_name"],
            "properties": {
                "item_name": {"type": "string"}
            }
        },
        "response_schemas": {"201": {}},
        "security_requirements": [],
        "tags": []
    }

    state_payload = {
        "run_id": "run_llm_merge_check",
        "endpoints": [endpoint],
        "test_plan": {
            "plan": {
                "create_item": {
                    "categories": ["functional"],
                    "target_case_count": {"functional": 2},
                    "priority": "high"
                }
            }
        }
    }

    # Mock successful LLM client returning 1 creative functional test
    mock_llm = AsyncMock()
    mock_llm.generate_structured.return_value = TestCaseLLMInputListModel(
        test_cases=[
            TestCaseLLMInputModel(
                endpoint_operation_id="create_item",
                category="functional",
                name="LLM Creative - Premium Gold Item",
                request={
                    "method": "POST",
                    "path": "/items",
                    "headers": {"X-Custom": "test"},
                    "query_params": {},
                    "path_params": {},
                    "body": {"item_name": "Premium Gold Widget"}
                },
                expected={"status_code": 201, "max_latency_ms": 3000.0},
                priority="high",
                generated_by="functional_generator_v1",
                rationale="Test premium item creation flow"
            )
        ]
    )

    res = await generate_category_test_cases(
        category="functional",
        prompt_filename="functional_v1.md",
        state_or_payload=state_payload,
        llm_client=mock_llm
    )

    tcs = res.get("test_cases", [])
    assert len(tcs) == 2  # 1 LLM case + 1 deterministic case padding to reach target_count=2

    # Check sources
    generators = [tc.get("generated_by") for tc in tcs]
    assert "functional_generator_v1" in generators
    assert "deterministic_payload_generator_v1" in generators

    # Check no generation_errors
    assert "generation_errors" not in res or len(res.get("generation_errors", [])) == 0


@pytest.mark.asyncio
async def test_two_track_execution_planning_and_token_propagation():
    """Verify execution planner groups happy-path functional flow into Track 1 and isolated negatives into Track 2."""
    from app.agents.execution.execution_planner import execution_planner_node, sort_test_cases_by_dependency

    tcs = [
        # Negative test that would break state if executed mid-stream
        {
            "id": "tc_neg_01",
            "endpoint_operation_id": "register_user",
            "category": "negative",
            "name": "Register User - Invalid Session ID",
            "request": {"method": "POST", "path": "/api/users/register", "body": {"session_id": "invalid_session"}},
            "expected": {"status_code": 400}
        },
        # Functional Step 1: Request OTP
        {
            "id": "tc_func_01",
            "endpoint_operation_id": "request_otp",
            "category": "functional",
            "name": "Request OTP - Valid Email",
            "request": {"method": "POST", "path": "/api/users/request-otp", "body": {"identifier": "alex@example.com", "type": "email"}},
            "expected": {"status_code": 200}
        },
        # Functional Step 2: Verify OTP
        {
            "id": "tc_func_02",
            "endpoint_operation_id": "verify_primary_otp",
            "category": "functional",
            "name": "Verify Primary OTP",
            "request": {"method": "POST", "path": "/api/users/verify-otp", "body": {"session_id": "placeholder_sess", "otp": "123456"}},
            "expected": {"status_code": 200}
        },
        # Functional Step 3: Register User
        {
            "id": "tc_func_03",
            "endpoint_operation_id": "register_user",
            "category": "functional",
            "name": "Register User - Valid Full Profile",
            "request": {"method": "POST", "path": "/api/users/register", "body": {"first_name": "Alexander", "last_name": "Johnson", "session_id": "placeholder_sess"}},
            "expected": {"status_code": 200}
        },
        # Negative Step: Request OTP with invalid phone
        {
            "id": "tc_neg_02",
            "endpoint_operation_id": "request_otp",
            "category": "negative",
            "name": "Request OTP - Invalid Phone Length",
            "request": {"method": "POST", "path": "/api/users/request-otp", "body": {"identifier": "123", "type": "phone"}},
            "expected": {"status_code": 400}
        }
    ]

    # Test sorting
    sorted_tcs = sort_test_cases_by_dependency(tcs)
    # Track 1 (functional) should be first: tc_func_01 -> tc_func_02 -> tc_func_03
    sorted_ids = [tc["id"] for tc in sorted_tcs]
    assert sorted_ids[0] == "tc_func_01"
    assert sorted_ids[1] == "tc_func_02"
    assert sorted_ids[2] == "tc_func_03"
    # Track 2 (negative) should be placed after functional
    assert sorted_ids[3:] == ["tc_neg_01", "tc_neg_02"] or set(sorted_ids[3:]) == {"tc_neg_01", "tc_neg_02"}

    # Test batch planning
    plan_res = await execution_planner_node({"test_cases": tcs})
    batches = plan_res.get("execution_batches", [])
    assert len(batches) >= 2
    assert batches[0]["track"] == "functional"
    assert batches[1]["track"] == "negative_isolated"

