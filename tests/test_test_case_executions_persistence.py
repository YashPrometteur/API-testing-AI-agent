import pytest
import asyncio
from app.database import get_mongo_db
from app.agents.reporting.reporting_agent import reporting_agent_node
from app.graph.state import TestRunState

@pytest.mark.asyncio
async def test_reporting_agent_persists_test_case_executions():
    db = await get_mongo_db()
    run_id = "test_run_persistence_verification_002"

    await db["runs"].replace_one(
        {"_id": run_id},
        {"_id": run_id, "spec_id": "spec_test", "status": "running"},
        upsert=True
    )

    state: TestRunState = {
        "run_id": run_id,
        "endpoints": [
            {
                "operation_id": "addPet",
                "path": "/pet",
                "method": "POST",
                "parameters": [],
                "request_schema": {},
                "response_schemas": {"200": {"description": "success"}},
                "security_requirements": [],
                "tags": ["pet"]
            },
            {
                "operation_id": "getPetById",
                "path": "/pet/{petId}",
                "method": "GET",
                "parameters": [],
                "request_schema": {},
                "response_schemas": {"200": {"description": "success"}},
                "security_requirements": [],
                "tags": ["pet"]
            }
        ],
        "test_cases": [
            {
                "id": "tc_002",
                "endpoint_operation_id": "getPetById",
                "category": "negative",
                "name": "Get Pet Fail Test",
                "request": {"method": "GET", "path": "/pet/99999"},
                "expected": {"status_code": 200},
                "priority": "medium",
                "generated_by": "negative_generator_v1",
                "rationale": "Validates error response"
            },
            {
                "id": "tc_001",
                "endpoint_operation_id": "addPet",
                "category": "functional",
                "name": "Add Pet Test",
                "request": {"method": "POST", "path": "/pet", "body": {"name": "Fluffy"}},
                "expected": {"status_code": 200},
                "priority": "high",
                "generated_by": "functional_generator_v1",
                "rationale": "Validates happy path pet creation"
            }
        ],
        "execution_results": [
            {
                "test_case_id": "tc_001",
                "status": "pass",
                "actual_status_code": 200,
                "actual_body": {"id": 1, "name": "Fluffy"},
                "actual_headers": {"content-type": "application/json"},
                "latency_ms": 120.5,
                "schema_valid": True,
                "assertion_failures": [],
                "attempt_count": 1,
                "executed_at": "2026-08-06T10:00:00Z"
            },
            {
                "test_case_id": "tc_002",
                "status": "flaky",
                "actual_status_code": 500,
                "actual_body": {"error": "Server error"},
                "actual_headers": {"content-type": "application/json"},
                "latency_ms": 340.2,
                "schema_valid": False,
                "assertion_failures": ["Status code mismatch"],
                "attempt_count": 2,
                "executed_at": "2026-08-06T10:01:00Z"
            }
        ],
        "findings": [
            {
                "id": "finding_002",
                "test_case_id": "tc_002",
                "severity": "high",
                "confidence": 0.95,
                "rationale": "Server error on invalid ID",
                "recommendation": "Return 404 instead of 500",
                "root_cause_summary": "Unhandled null pointer exception",
                "suggested_fix": "Add null check",
                "supporting_evidence": ["Traceback line 42"],
                "created_at": "2026-08-06T10:01:00Z"
            }
        ],
        "retry_counts": {"tc_002": 1},
        "reflection_count": 1,
        "status": "running"
    }

    # Execute reporting agent node
    res = await reporting_agent_node(state)
    assert res.get("status") == "completed"

    # Fetch updated run document directly from MongoDB
    run_doc = await db["runs"].find_one({"_id": run_id})
    assert run_doc is not None
    assert "test_case_executions" in run_doc
    executions = run_doc["test_case_executions"]
    assert len(executions) == 2

    # Assert deterministic sorting: functional (category) comes before negative
    func_entry = executions[0]
    neg_entry = executions[1]

    assert func_entry["category"] == "functional"
    assert func_entry["test_case_id"] == "tc_001"
    assert func_entry["endpoint_operation_id"] == "addPet"
    assert func_entry["endpoint_path"] == "/pet"
    assert func_entry["http_method"] == "POST"
    assert func_entry["endpoint"] == "POST /pet"
    assert func_entry["execution_status"] == "PASS"
    assert func_entry["priority"] == "high"
    assert func_entry["generated_by"] == "functional_generator_v1"

    assert neg_entry["category"] == "negative"
    assert neg_entry["test_case_id"] == "tc_002"
    assert neg_entry["endpoint"] == "GET /pet/99999"
    assert neg_entry["execution_status"] == "FLAKY"
    # Assert human-readable failure_reason generated when missing
    assert neg_entry["execution_result"]["failure_reason"] == "Expected HTTP 200 but received HTTP 500."
    # Assert complete finding fields preserved
    assert neg_entry["finding"]["root_cause_summary"] == "Unhandled null pointer exception"
    assert neg_entry["finding"]["suggested_fix"] == "Add null check"
    # Assert retry history present
    assert "history" in neg_entry
    assert len(neg_entry["history"]) == 2

    # Cleanup
    await db["runs"].delete_one({"_id": run_id})
