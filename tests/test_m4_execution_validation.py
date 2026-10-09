import asyncio
import datetime
import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
import httpx

from app.agents.execution.execution_planner import (
    execution_planner_node,
    sort_test_cases_by_dependency,
)
from app.agents.execution.execution_agents import execute_batch_node
from app.agents.validation.response_validation_agent import response_validation_node
from app.graph.build import build_graph
from app.graph.state import TestCase, TestRunState
from app.tools.assertion_engine import AssertionEngineTool
from app.tools.circuit_breaker import HostCircuitBreaker
from app.tools.http_executor import HTTPExecutionTool
from app.tools.schema_validator import SchemaValidatorTool


# 1. Dependency Ordering Unit Test
def test_execution_planner_dependency_ordering():
    tc_create = TestCase(
        id="tc_func_create_001",
        endpoint_operation_id="addPet",
        category="functional",
        name="Create Pet",
        request={"method": "POST", "path": "/pet", "body": {"name": "Fido"}},
        expected={"status_code": 201},
        priority="high",
        generated_by="functional_v1",
        rationale="Creates pet"
    )

    tc_get = TestCase(
        id="tc_func_get_001",
        endpoint_operation_id="getPetById",
        category="functional",
        name="Get Pet",
        request={"method": "GET", "path": "/pet/{petId}", "path_params": {"petId": 123}},
        expected={"status_code": 200},
        priority="medium",
        generated_by="functional_v1",
        rationale="Gets pet by ID"
    )

    tc_delete = TestCase(
        id="tc_func_delete_001",
        endpoint_operation_id="deletePet",
        category="functional",
        name="Delete Pet",
        request={"method": "DELETE", "path": "/pet/{petId}", "path_params": {"petId": 123}},
        expected={"status_code": 200},
        priority="high",
        generated_by="functional_v1",
        rationale="Deletes pet by ID"
    )

    # Reverse order in input list
    input_cases = [tc_get, tc_delete, tc_create]
    sorted_cases = sort_test_cases_by_dependency(input_cases)

    # Assert POST create runs FIRST before GET and DELETE dependents
    assert sorted_cases[0]["id"] == "tc_func_create_001"
    assert sorted_cases[-1]["id"] in ("tc_func_get_001", "tc_func_delete_001")


# 2. HTTP Executor Transient Retry & Backoff Unit Test
@pytest.mark.asyncio
async def test_http_executor_transient_retry_and_backoff(monkeypatch):
    calls = []

    async def mock_handler(request: httpx.Request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)

    async def mock_sleep(secs):
        pass

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    res = await HTTPExecutionTool.execute(
        method="GET",
        url="http://mockhost/test",
        expected_status_code=200,
        max_retries=3,
        client=client
    )

    assert res.success is True
    assert res.data["status_code"] == 200
    assert res.data["attempt_count"] == 3
    assert len(calls) == 3


# 3. HTTP Executor 4xx Negative Expected Outcome Unit Test
@pytest.mark.asyncio
async def test_http_executor_negative_4xx_no_retry():
    calls = []

    async def mock_handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(400, json={"error": "Invalid payload"})

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)

    res = await HTTPExecutionTool.execute(
        method="POST",
        url="http://mockhost/pet",
        expected_status_code=400,
        max_retries=3,
        client=client
    )

    # 400 matching expected status code 400 MUST NOT be retried (attempt_count == 1)
    assert res.success is True
    assert res.data["status_code"] == 400
    assert res.data["attempt_count"] == 1
    assert len(calls) == 1


# 4. Circuit Breaker Unit Test
def test_circuit_breaker_tripping():
    cb = HostCircuitBreaker(max_failures=5)
    host = "http://api.brokenhost.com/v1/test"

    for _ in range(4):
        cb.record_transient_failure(host)
        assert cb.is_open(host) is False

    # 5th consecutive transient failure trips breaker
    tripped = cb.record_transient_failure(host)
    assert tripped is True
    assert cb.is_open(host) is True

    # Reset on success
    cb.record_success(host)
    assert cb.is_open(host) is False


# 5. Schema Validator Unit Test (Actual Status Code Schema Matching)
def test_schema_validator_actual_status_code():
    body_200 = {"id": 1, "name": "Fido"}
    schema_200 = {
        "type": "object",
        "required": ["id", "name"],
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}
    }

    res = SchemaValidatorTool.validate(body_200, schema_200)
    assert res.success is True
    assert res.data["valid"] is True

    # Malformed error body tested against 500 schema
    body_500_invalid = {"bad_error": 123}
    schema_500 = {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}}
    }

    res_500 = SchemaValidatorTool.validate(body_500_invalid, schema_500)
    assert res_500.success is True
    assert res_500.data["valid"] is False
    assert len(res_500.data["errors"]) > 0


# 6. Assertion Engine Unit Test
def test_assertion_engine_evaluations():
    expected = {"status_code": 200, "response_schema_match": True}
    res_pass = AssertionEngineTool.evaluate(
        actual_status_code=200,
        actual_headers={"content-type": "application/json"},
        actual_body={"name": "Fido"},
        latency_ms=45.0,
        expected=expected,
        schema_valid=True
    )
    assert res_pass.data["passed"] is True
    assert len(res_pass.data["assertion_failures"]) == 0

    # Status code mismatch test
    res_fail = AssertionEngineTool.evaluate(
        actual_status_code=500,
        actual_headers={},
        actual_body={"error": "Internal Error"},
        latency_ms=120.0,
        expected=expected,
        schema_valid=True
    )
    assert res_fail.data["passed"] is False
    assert any("Status code mismatch" in err for err in res_fail.data["assertion_failures"])


# 7. Response Validation Node Unit Test
@pytest.mark.asyncio
async def test_response_validation_node():
    test_cases = [
        TestCase(
            id="tc_001",
            endpoint_operation_id="addPet",
            category="functional",
            name="Add Pet",
            request={"method": "POST", "path": "/pet"},
            expected={"status_code": 200, "response_schema_match": True},
            priority="high",
            generated_by="functional_v1",
            rationale="Test"
        )
    ]

    endpoints = [
        {
            "operation_id": "addPet",
            "path": "/pet",
            "method": "POST",
            "parameters": [],
            "request_schema": None,
            "response_schemas": {
                "200": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "integer"}}
                }
            },
            "security_requirements": [],
            "tags": []
        }
    ]

    exec_results = [
        {
            "test_case_id": "tc_001",
            "status": "pass",
            "actual_status_code": 200,
            "actual_body": {"id": 100},
            "latency_ms": 30.0,
            "attempt_count": 1,
            "schema_valid": None,
            "assertion_failures": [],
            "executed_at": "2026-07-29T12:00:00Z"
        }
    ]

    state = {
        "test_cases": test_cases,
        "endpoints": endpoints,
        "execution_results": exec_results
    }

    out = await response_validation_node(state)
    validated = out.get("execution_results", [])

    assert len(validated) == 1
    assert validated[0]["status"] == "pass"
    assert validated[0]["schema_valid"] is True


# 8. Full Graph End-to-End Execution Fan-out Integration Test against Mock Server
@pytest.mark.asyncio
async def test_full_m4_execution_graph_fan_out(monkeypatch):
    mock_app = FastAPI()

    @mock_app.post("/pet")
    async def add_pet():
        return {"id": 1, "name": "Fido", "status": "available"}

    @mock_app.get("/pet/{pet_id}")
    async def get_pet(pet_id: int):
        return {"id": pet_id, "name": "Fido", "status": "available"}

    @mock_app.delete("/pet/{pet_id}")
    async def delete_pet(pet_id: int):
        return {"id": pet_id, "message": "deleted"}

    client = TestClient(mock_app)

    async def mock_execute(method, url, headers=None, query_params=None, body=None, expected_status_code=None, max_retries=3, client=None, **kwargs):
        req_kwargs = {}
        if body and isinstance(body, dict):
            req_kwargs["json"] = body

        resp = getattr(client or TestClient(mock_app), method.lower())(url, **req_kwargs)
        try:
            resp_body = resp.json()
        except Exception:
            resp_body = resp.text

        print(f"[MOCK_EXECUTE] {method} {url} -> status={resp.status_code} body={resp_body}")

        return type("MockToolResult", (), {
            "success": True,
            "data": {
                "status_code": resp.status_code,
                "headers": dict(resp.headers),
                "body": resp_body,
                "latency_ms": 15.0,
                "attempt_count": 1,
                "transient_error": False
            }
        })()

    monkeypatch.setattr(HTTPExecutionTool, "execute", mock_execute)

    import app.graph.build as build_module

    # Mock discovery and planner agents inside build module
    monkeypatch.setattr(build_module, "DiscoveryAgent", lambda *args, **kwargs: lambda state: {})
    monkeypatch.setattr(build_module, "PlannerAgent", lambda *args, **kwargs: lambda state: {})

    # Mock all generator nodes inside build module
    monkeypatch.setattr(build_module, "functional_generator_node", lambda state, *args, **kwargs: {"test_cases": []})
    monkeypatch.setattr(build_module, "negative_generator_node", lambda state, *args, **kwargs: {"test_cases": []})
    monkeypatch.setattr(build_module, "edge_case_generator_node", lambda state, *args, **kwargs: {"test_cases": []})
    monkeypatch.setattr(build_module, "security_generator_node", lambda state, *args, **kwargs: {"test_cases": []})
    monkeypatch.setattr(build_module, "auth_generator_node", lambda state, *args, **kwargs: {"test_cases": []})
    monkeypatch.setattr(build_module, "authz_generator_node", lambda state, *args, **kwargs: {"test_cases": []})
    monkeypatch.setattr(build_module, "regression_generator_node", lambda state, *args, **kwargs: {"test_cases": []})

    # Define test plan and test cases
    endpoints = [
        {"operation_id": "addPet", "path": "/pet", "method": "POST", "parameters": [], "request_schema": None, "response_schemas": {"200": {"type": "object"}}, "security_requirements": [], "tags": []},
        {"operation_id": "getPetById", "path": "/pet/{petId}", "method": "GET", "parameters": [], "request_schema": None, "response_schemas": {"200": {"type": "object"}}, "security_requirements": [], "tags": []},
        {"operation_id": "deletePet", "path": "/pet/{petId}", "method": "DELETE", "parameters": [], "request_schema": None, "response_schemas": {"200": {"type": "object"}}, "security_requirements": [], "tags": []}
    ]

    test_plan = {
        "plan": {
            "addPet": {"categories": ["functional"], "target_case_count": {"functional": 1}, "priority": "high"},
            "getPetById": {"categories": ["functional"], "target_case_count": {"functional": 1}, "priority": "medium"},
            "deletePet": {"categories": ["functional"], "target_case_count": {"functional": 1}, "priority": "high"}
        }
    }

    test_cases = [
        TestCase(id="tc_get_001", endpoint_operation_id="getPetById", category="functional", name="Get Pet", request={"method": "GET", "path": "/pet/1"}, expected={"status_code": 200}, priority="medium", generated_by="functional_v1", rationale="Get"),
        TestCase(id="tc_delete_001", endpoint_operation_id="deletePet", category="functional", name="Delete Pet", request={"method": "DELETE", "path": "/pet/1"}, expected={"status_code": 200}, priority="high", generated_by="functional_v1", rationale="Delete"),
        TestCase(id="tc_post_001", endpoint_operation_id="addPet", category="functional", name="Add Pet", request={"method": "POST", "path": "/pet", "body": {"name": "Fido"}}, expected={"status_code": 200}, priority="high", generated_by="functional_v1", rationale="Create")
    ]

    # Run state graph starting at execution_planner
    graph = build_graph()

    # Stub Mock LLM for discovery & planning if needed
    import app.agents.planner.planner_agent as planner_module
    class MockPlanner:
        async def generate_structured(self, *args, **kwargs):
            from app.models.test_plan import EndpointPlanModel, TestPlan
            return TestPlan(plan={"addPet": EndpointPlanModel(categories=["functional"], target_case_count={"functional": 1}, priority="high")})
    monkeypatch.setattr(planner_module, "get_llm_client", lambda: MockPlanner())

    initial_state = {
        "run_id": "run_m4_test_001",
        "spec_source": {"openapi": "3.0.0"},
        "status": "running",
        "endpoints": endpoints,
        "test_plan": test_plan,
        "test_cases": test_cases,
        "target_base_url": "http://testserver",
        "execution_results": [],
        "reflection_count": 0,
        "retry_counts": {},
        "errors": []
    }

    import uuid
    unique_thread_id = f"m4_test_thread_{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": unique_thread_id}}
    res = await graph.ainvoke(initial_state, config=config)

    assert res.get("status") == "completed"
    exec_results = res.get("execution_results", [])
    assert len(exec_results) == 3

    # All execution results must be passed
    for er in exec_results:
        assert er["status"] == "pass"
        assert er["schema_valid"] is True
        assert len(er["assertion_failures"]) == 0


# 9. Merge Execution Results Reducer Unit Test
def test_merge_execution_results_partial_updates():
    from app.graph.state import merge_execution_results

    left = [
        {
            "test_case_id": "tc_001",
            "status": "pass",
            "actual_status_code": 200,
            "actual_body": {"id": 1, "name": "Fido"},
            "latency_ms": 12.5,
            "attempt_count": 1,
            "executed_at": "2026-07-29T12:00:00Z"
        }
    ]

    right = [
        {
            "test_case_id": "tc_001",
            "schema_valid": True,
            "assertion_failures": []
        }
    ]

    merged = merge_execution_results(left, right)

    assert len(merged) == 1
    res = merged[0]
    assert res["test_case_id"] == "tc_001"
    assert res["actual_status_code"] == 200
    assert res["schema_valid"] is True
    assert res["actual_body"] == {"id": 1, "name": "Fido"}
    assert res["assertion_failures"] == []


# 10. Host Circuit Breaker Full Recovery Unit Test
def test_circuit_breaker_full_recovery():
    cb = HostCircuitBreaker(max_failures=3)
    host = "api.brokenhost.com"

    # Trip the circuit
    cb.record_transient_failure(host)
    cb.record_transient_failure(host)
    cb.record_transient_failure(host)
    assert cb.is_open(host) is True

    # Successful request clears state and closes the circuit
    cb.record_success(host)
    assert cb.is_open(host) is False

    # Verify that the consecutive failure count reset (needs 3 consecutive failures to trip again)
    cb.record_transient_failure(host)
    assert cb.is_open(host) is False
    cb.record_transient_failure(host)
    assert cb.is_open(host) is False
    cb.record_transient_failure(host)
    assert cb.is_open(host) is True


def test_circuit_breaker_half_open_probe_and_recovery(monkeypatch):
    """Verify half-open probe allows recovery after cooldown, and re-trips on failed probe."""
    import time
    fake_time = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_time)

    cb = HostCircuitBreaker(max_failures=3, cooldown_seconds=30.0)
    host = "api.probehost.com"

    # 1. Trip breaker
    cb.record_transient_failure(host)
    cb.record_transient_failure(host)
    cb.record_transient_failure(host)
    assert cb.is_open(host) is True

    # 2. Before cooldown (15s elapsed): still open
    fake_time += 15.0
    assert cb.is_open(host) is True

    # 3. After cooldown (31s elapsed): enters half-open, is_open returns False to allow probe
    fake_time += 16.0  # total 31s elapsed
    assert cb.is_open(host) is False

    # 4. Probe succeeds -> fully closes breaker and resets failures
    cb.record_success(host)
    assert cb.is_open(host) is False
    assert cb._failure_counts.get(host, 0) == 0

    # 5. Now trip again to test failed probe path
    cb.record_transient_failure(host)
    cb.record_transient_failure(host)
    cb.record_transient_failure(host)
    assert cb.is_open(host) is True

    # 6. Cooldown elapses (35s) -> probe allowed
    fake_time += 35.0
    assert cb.is_open(host) is False

    # 7. Probe fails -> re-trips immediately with fresh cooldown timer
    cb.record_transient_failure(host)
    assert cb.is_open(host) is True

    # 8. Advance 10s (under new cooldown): still open
    fake_time += 10.0
    assert cb.is_open(host) is True

    # 9. Advance another 25s (total 35s under new cooldown): probe allowed again
    fake_time += 25.0
    assert cb.is_open(host) is False



