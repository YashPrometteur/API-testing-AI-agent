# pyrefly: ignore [missing-import]
import pytest
import shutil
import os
import sys

sys.path.insert(0, ".")

from app.graph.state import TestRunState, Finding, ExecutionResult
from app.tools.rag_retriever import RAGRetrieverTool, get_chroma_client
from app.agents.reflection.reflection_agent import reflection_agent_node
from app.graph.routers import reflection_router


@pytest.fixture(autouse=True)
def setup_test_db():
    """Setup clean state before each test."""
    # Ensure any temporary DB folder is wiped if it exists
    db_path = "./chroma_db"
    if os.path.exists(db_path):
        try:
            shutil.rmtree(db_path)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_rag_partitioning():
    """Verify that retrieval respects partitioning and does not bleed namespaces."""
    client = get_chroma_client()
    
    # 1. Upsert a unique test document into 'past_defects' collection
    defect_col = client.get_or_create_collection("past_defects")
    defect_col.upsert(
        ids=["defect_test_1"],
        documents=["Defect description: Null pointer exception during deletion query."],
        embeddings=[[0.1] * 384]
    )

    # 2. Upsert a unique test document into 'api_docs' collection
    docs_col = client.get_or_create_collection("api_docs")
    docs_col.upsert(
        ids=["doc_test_1"],
        documents=["API spec: DELETE /pet/{petId} returns 200 OK or 400 Bad Request."],
        embeddings=[[0.9] * 384]
    )

    # 3. Retrieve from 'past_defects' only and assert we get the defect doc but NOT the api doc
    res_defects = await RAGRetrieverTool.retrieve(query="deletion query", top_k=2, doc_type="past_defects")
    assert res_defects.success
    docs = [item[0] for item in res_defects.data]
    ids = [item[1] for item in res_defects.data]
    assert "defect_test_1" in ids
    assert "doc_test_1" not in ids

    # 4. Retrieve from 'api_docs' only and assert the reverse
    res_docs = await RAGRetrieverTool.retrieve(query="DELETE /pet", top_k=2, doc_type="api_docs")
    assert res_docs.success
    doc_ids = [item[1] for item in res_docs.data]
    assert "doc_test_1" in doc_ids
    assert "defect_test_1" not in doc_ids


@pytest.mark.asyncio
async def test_reflection_loop_limit():
    """Verify reflection loop terminates at max_reflection_loops=2."""
    test_cases = [
        {
            "id": "tc_001",
            "endpoint_operation_id": "deletePet",
            "category": "negative",
            "request": {"path": "/pet/123", "method": "DELETE"},
            "expected": {"status_code": 400}
        }
    ]

    findings = [
        {
            "id": "find_1",
            "test_case_id": "tc_001",
            "severity": "high",
            "confidence": 0.3,  # confidence < 0.5 (triggers reflection)
            "root_cause_summary": "Bad path format",
            "suggested_fix": "Fix path params",
            "supporting_evidence": []
        }
    ]

    # Single failed execution result (attempt_count = 1)
    execution_results = [
        {
            "test_case_id": "tc_001",
            "status": "fail",
            "attempt_count": 1,
            "actual_status_code": 500,
            "actual_body": "Internal Error",
            "latency_ms": 150.0,
            "schema_valid": True,
            "assertion_failures": ["Status code mismatch"]
        }
    ]

    # Loop 1: reflection_count = 0
    state_0: TestRunState = {
        "test_cases": test_cases,
        "findings": findings,
        "execution_results": execution_results,
        "reflection_count": 0,
        "route_decision": None,
        "status": "running"
    }

    res_state_1 = await reflection_agent_node(state_0)
    assert res_state_1["reflection_count"] == 1
    assert res_state_1["route_decision"] == "loop_back"
    assert reflection_router(res_state_1) == "loop_back_to_execution"

    # Loop 2: reflection_count = 1
    state_1: TestRunState = {
        "test_cases": test_cases,
        "findings": findings,
        "execution_results": execution_results,
        "reflection_count": 1,
        "route_decision": None,
        "status": "running"
    }

    res_state_2 = await reflection_agent_node(state_1)
    assert res_state_2["reflection_count"] == 2
    assert res_state_2["route_decision"] == "loop_back"
    assert reflection_router(res_state_2) == "loop_back_to_execution"

    # Loop 3: reflection_count = 2 (should exit loop)
    state_2: TestRunState = {
        "test_cases": test_cases,
        "findings": findings,
        "execution_results": execution_results,
        "reflection_count": 2,
        "route_decision": None,
        "status": "running"
    }

    res_state_3 = await reflection_agent_node(state_2)
    # Count should not increment beyond 2
    assert res_state_3.get("reflection_count", 2) == 2
    assert res_state_3["route_decision"] == "proceed_to_guardrail"
    assert reflection_router(res_state_3) == "proceed_to_guardrail"


@pytest.mark.asyncio
async def test_flaky_test_status_detection():
    """Verify that a test case flipping between pass/fail status is marked 'flaky'."""
    test_cases = [
        {
            "id": "tc_flaky",
            "endpoint_operation_id": "updatePet",
            "category": "functional"
        }
    ]

    # Two runs for same test case: first pass, second fail
    execution_results = [
        {
            "test_case_id": "tc_flaky",
            "status": "pass",
            "attempt_count": 1
        },
        {
            "test_case_id": "tc_flaky",
            "status": "fail",
            "attempt_count": 2
        }
    ]

    state: TestRunState = {
        "test_cases": test_cases,
        "findings": [],
        "execution_results": execution_results,
        "reflection_count": 0,
        "route_decision": None,
        "status": "running"
    }

    res = await reflection_agent_node(state)
    updated_runs = res["execution_results"]

    # All runs for this test case should be marked 'flaky'
    assert len(updated_runs) == 2
    assert updated_runs[0]["status"] == "flaky"
    assert updated_runs[1]["status"] == "flaky"


@pytest.mark.asyncio
async def test_reflection_failsafe_on_exception():
    """Verify reflection agent node defaults to proceed_to_guardrail on exceptions."""
    # Pass bad/corrupted state values (e.g. findings is None to raise TypeError)
    bad_state = {
        "test_cases": [],
        "findings": None,  # Will raise TypeError when iterating
        "execution_results": [],
        "reflection_count": 0,
        "status": "running"
    }

    res = await reflection_agent_node(bad_state)
    assert res["route_decision"] == "proceed_to_guardrail"
    assert reflection_router(res) == "proceed_to_guardrail"


def test_findings_deduplication_on_merge():
    """Regression test: Verify merge_findings prevents duplicate finding IDs after reflection/re-execution."""
    from app.graph.state import merge_findings, Finding

    finding1: Finding = {
        "id": "find_de125b35",
        "test_case_id": "tc_1",
        "severity": "high",
        "confidence": 0.9,
        "root_cause_summary": "Summary 1",
        "suggested_fix": "Fix 1",
        "supporting_evidence": [],
        "is_duplicate": False,
        "guardrail_status": "pending"
    }

    finding1_updated: Finding = {
        **finding1,
        "confidence": 0.95
    }

    finding2: Finding = {
        "id": "find_42cfd647",
        "test_case_id": "tc_2",
        "severity": "medium",
        "confidence": 0.8,
        "root_cause_summary": "Summary 2",
        "suggested_fix": "Fix 2",
        "supporting_evidence": [],
        "is_duplicate": False,
        "guardrail_status": "pending"
    }

    # Simulate Attempt 1 state findings
    left = [finding1, finding2]
    # Simulate Attempt 2 re-execution return from RCA / Guardrail node
    right = [finding1_updated, finding2]

    merged = merge_findings(left, right)

    # Must contain exactly 2 unique findings, not 4
    assert len(merged) == 2
    finding_ids = [f["id"] for f in merged]
    assert sorted(finding_ids) == ["find_42cfd647", "find_de125b35"]
    
    # Updated finding attributes (e.g. confidence) should be preserved
    merged_f1 = next(f for f in merged if f["id"] == "find_de125b35")
    assert merged_f1["confidence"] == 0.95


@pytest.mark.asyncio
async def test_reflection_selective_re_execution():
    """Regression test: Verify that reflection re-executes ONLY failed test cases.
    
    Given 3 test cases: tc_pass_1 (PASS), tc_fail_1 (FAIL), tc_pass_2 (PASS)
    When execution_planner_node runs during a reflection loop (reflection_count = 1),
    Then ONLY tc_fail_1 is included in the execution batches.
    PASS test cases remain at attempt_count = 1.
    """
    from app.agents.execution.execution_planner import execution_planner_node
    from app.graph.state import merge_execution_results, ExecutionResult, TestCase

    tc_pass_1: TestCase = {"id": "tc_pass_1", "name": "Pass Test 1", "endpoint_operation_id": "getPet", "category": "functional"}
    tc_fail_1: TestCase = {"id": "tc_fail_1", "name": "Fail Test 1", "endpoint_operation_id": "addPet", "category": "functional"}
    tc_pass_2: TestCase = {"id": "tc_pass_2", "name": "Pass Test 2", "endpoint_operation_id": "getPet", "category": "functional"}

    attempt1_results: List[ExecutionResult] = [
        {"test_case_id": "tc_pass_1", "status": "pass", "actual_status_code": 200, "latency_ms": 10.0, "attempt_count": 1, "schema_valid": True, "assertion_failures": [], "executed_at": "2026-08-14T00:00:00Z"},
        {"test_case_id": "tc_fail_1", "status": "fail", "actual_status_code": 500, "latency_ms": 15.0, "attempt_count": 1, "schema_valid": False, "assertion_failures": ["Server Error"], "executed_at": "2026-08-14T00:00:00Z"},
        {"test_case_id": "tc_pass_2", "status": "pass", "actual_status_code": 200, "latency_ms": 12.0, "attempt_count": 1, "schema_valid": True, "assertion_failures": [], "executed_at": "2026-08-14T00:00:00Z"}
    ]

    reflection_state = {
        "run_id": "run_selective_test",
        "test_cases": [tc_pass_1, tc_fail_1, tc_pass_2],
        "execution_results": attempt1_results,
        "reflection_count": 1,
        "route_decision": "loop_back",
        "status": "running"
    }

    # 1. Run execution_planner_node for reflection loop
    planner_res = await execution_planner_node(reflection_state)
    batches = planner_res.get("execution_batches", [])

    # Verify ONLY tc_fail_1 is planned in the batch
    planned_tcs = [tc for b in batches for tc in b.get("test_cases", [])]
    planned_ids = [tc["id"] for tc in planned_tcs]
    assert planned_ids == ["tc_fail_1"]

    # 2. Simulate re-execution of tc_fail_1 (Attempt 2 PASS -> FLAKY)
    re_exec_raw: ExecutionResult = {
        "test_case_id": "tc_fail_1",
        "status": "pending_validation",
        "actual_status_code": 200,
        "latency_ms": 8.0,
        "attempt_count": 1,
        "schema_valid": None,
        "assertion_failures": [],
        "executed_at": "2026-08-14T00:01:00Z"
    }
    merged_after_re_exec = merge_execution_results(attempt1_results, [re_exec_raw])

    # tc_pass_1 and tc_pass_2 must remain at attempt_count = 1 and status = "pass"
    pass_1 = next(r for r in merged_after_re_exec if r["test_case_id"] == "tc_pass_1")
    pass_2 = next(r for r in merged_after_re_exec if r["test_case_id"] == "tc_pass_2")
    fail_1 = next(r for r in merged_after_re_exec if r["test_case_id"] == "tc_fail_1")

    assert pass_1["attempt_count"] == 1
    assert pass_1["status"] == "pass"
    assert pass_2["attempt_count"] == 1
    assert pass_2["status"] == "pass"
    assert fail_1["attempt_count"] == 2

    # 3. Simulate validation of tc_fail_1 (PASS result -> status becomes "flaky")
    validated_re_exec: ExecutionResult = {
        "test_case_id": "tc_fail_1",
        "status": "pass",
        "actual_status_code": 200,
        "latency_ms": 8.0,
        "attempt_count": 2,
        "schema_valid": True,
        "assertion_failures": [],
        "executed_at": "2026-08-14T00:01:00Z"
    }
    final_merged = merge_execution_results(merged_after_re_exec, [validated_re_exec])

    final_fail_1 = next(r for r in final_merged if r["test_case_id"] == "tc_fail_1")
    assert final_fail_1["status"] == "flaky"
    assert final_fail_1["attempt_count"] == 2


@pytest.mark.asyncio
async def test_rca_node_reuses_existing_finding_id_across_reflection_passes():
    """Verify rca_node reuses the existing finding_id when re-diagnosing a test case across reflection passes."""
    from app.agents.rca.rca_agent import rca_node
    from app.graph.state import merge_findings

    test_case = {
        "id": "tc_fail_dup_test",
        "name": "Failing Test Case",
        "endpoint_operation_id": "getPetById",
        "category": "negative",
        "request": {"method": "GET", "path": "/pet/0"},
        "expected": {"status_code": 400}
    }

    exec_result: ExecutionResult = {
        "test_case_id": "tc_fail_dup_test",
        "status": "fail",
        "actual_status_code": 404,
        "actual_body": {"error": "Not Found"},
        "latency_ms": 50.0,
        "attempt_count": 1,
        "schema_valid": True,
        "assertion_failures": ["Status code mismatch: expected 400, got 404"],
        "executed_at": "2026-08-17T00:00:00Z"
    }

    state_pass1: TestRunState = {
        "test_cases": [test_case],
        "execution_results": [exec_result],
        "findings": [],
        "reflection_count": 0,
        "status": "running"
    }

    # Pass 1: Initial RCA execution creates a new finding
    res_pass1 = await rca_node(state_pass1)
    findings_pass1 = res_pass1.get("findings", [])
    assert len(findings_pass1) == 1
    original_finding_id = findings_pass1[0]["id"]
    assert original_finding_id.startswith("find_")

    merged_findings_pass1 = merge_findings([], findings_pass1)
    assert len(merged_findings_pass1) == 1

    # Pass 2: Reflection loop re-runs RCA with existing finding in state
    exec_result_attempt2: ExecutionResult = {
        **exec_result,
        "attempt_count": 2
    }
    state_pass2: TestRunState = {
        "test_cases": [test_case],
        "execution_results": [exec_result_attempt2],
        "findings": merged_findings_pass1,
        "reflection_count": 1,
        "status": "running"
    }

    res_pass2 = await rca_node(state_pass2)
    findings_pass2 = res_pass2.get("findings", [])
    assert len(findings_pass2) == 1
    assert findings_pass2[0]["id"] == original_finding_id
    assert findings_pass2[0]["test_case_id"] == "tc_fail_dup_test"

    # Merge reducer preserves single finding
    final_merged = merge_findings(merged_findings_pass1, findings_pass2)
    assert len(final_merged) == 1
    assert final_merged[0]["id"] == original_finding_id


@pytest.mark.parametrize(
    "proposed,category,actual_code,expected_code,expected_result",
    [
        # --- 1. Standard 4xx mismatches (Clamped to 'medium') ---
        ("high", "negative", 415, 400, "medium"),
        ("critical", "negative", 405, 400, "medium"),
        ("high", "functional", 404, 200, "medium"),
        ("high", "edge_case", 422, 400, "medium"),
        ("critical", "regression", 400, 200, "medium"),
        ("low", "negative", 415, 400, "low"),
        ("medium", "negative", 415, 400, "medium"),

        # --- 2. Security Category (NEVER clamped - retains high/critical) ---
        ("critical", "security", 200, 400, "critical"),
        ("high", "security", 200, 403, "high"),
        ("critical", "security", 404, 403, "critical"),
        ("high", "security", 415, 400, "high"),

        # --- 3. Auth & Authz Categories (NEVER clamped - retains high/critical) ---
        ("critical", "auth", 401, 200, "critical"),
        ("high", "auth", 403, 200, "high"),
        ("high", "authz", 403, 200, "high"),
        ("critical", "authz", 404, 403, "critical"),

        # --- 4. Server Crashes 5xx (NEVER clamped - retains high/critical) ---
        ("high", "negative", 500, 400, "high"),
        ("critical", "functional", 500, 200, "critical"),
        ("high", "edge_case", 502, 400, "high"),
        ("high", "functional", 503, 200, "high"),

        # --- 5. Network Timeouts / Connection Resets (actual_code is None or 0 - NEVER clamped) ---
        ("critical", "functional", None, 200, "critical"),
        ("high", "negative", None, 400, "high"),
        ("critical", "functional", 0, 200, "critical"),

        # --- 6. Invalid / Fallback inputs ---
        ("unknown_sev", "functional", 200, 200, "medium"),
        ("", "negative", 415, 400, "medium"),
        (None, "negative", 415, 400, "medium"),
    ]
)
def test_rca_normalize_severity_policy(proposed, category, actual_code, expected_code, expected_result):
    """Verify deterministic severity normalization across all boundary cases."""
    from app.agents.rca.rca_agent import normalize_severity

    result = normalize_severity(
        proposed_severity=proposed,
        category=category,
        actual_status_code=actual_code,
        expected_status_code=expected_code
    )
    assert result == expected_result, (
        f"Failed for proposed={proposed}, category={category}, actual_code={actual_code}. "
        f"Expected {expected_result}, got {result}"
    )


@pytest.mark.asyncio
async def test_rca_node_redetermines_on_failure_signature_change():
    """Verify rca_node re-diagnoses a finding when failure signature changes while keeping the same finding_id."""
    from app.agents.rca.rca_agent import rca_node

    test_case: TestCase = {
        "id": "tc_sig_change_test",
        "endpoint_operation_id": "updatePet",
        "path": "/pet",
        "method": "PUT",
        "category": "functional",
        "name": "Update Pet Test",
        "request": {"method": "PUT", "path": "/pet", "body": {"name": "Fluffy"}},
        "expected": {"status_code": 200, "response_schema_match": True},
        "priority": "high",
        "generated_by": "functional_v1",
        "rationale": "Verify updating pet"
    }

    # Pass 1: Initial failure with 500 Internal Server Error
    exec_result_pass1: ExecutionResult = {
        "test_case_id": "tc_sig_change_test",
        "status": "fail",
        "actual_status_code": 500,
        "actual_body": {"error": "Internal database crash"},
        "latency_ms": 120.0,
        "attempt_count": 1,
        "schema_valid": False,
        "assertion_failures": ["Internal Server Error 500"],
        "executed_at": "2026-08-18T00:00:00Z"
    }

    state_pass1: TestRunState = {
        "test_cases": [test_case],
        "execution_results": [exec_result_pass1],
        "findings": [],
        "reflection_count": 0,
        "status": "running"
    }

    res_pass1 = await rca_node(state_pass1)
    findings_pass1 = res_pass1.get("findings", [])
    assert len(findings_pass1) == 1
    fid = findings_pass1[0]["id"]
    assert findings_pass1[0]["diagnosed_status_code"] == 500
    assert findings_pass1[0]["diagnosed_failure_reason"] == "Internal Server Error 500"

    # Pass 2: Reflection attempt 2 fails with different signature (400 Bad Request)
    exec_result_pass2: ExecutionResult = {
        "test_case_id": "tc_sig_change_test",
        "status": "fail",
        "actual_status_code": 400,
        "actual_body": {"error": "Invalid pet payload"},
        "latency_ms": 40.0,
        "attempt_count": 2,
        "schema_valid": False,
        "assertion_failures": ["Status code mismatch: expected 200, got 400"],
        "executed_at": "2026-08-18T00:01:00Z"
    }

    state_pass2: TestRunState = {
        "test_cases": [test_case],
        "execution_results": [exec_result_pass2],
        "findings": findings_pass1,
        "reflection_count": 1,
        "status": "running"
    }

    res_pass2 = await rca_node(state_pass2)
    findings_pass2 = res_pass2.get("findings", [])
    assert len(findings_pass2) == 1
    # Finding ID must remain the same!
    assert findings_pass2[0]["id"] == fid
    # Failure signature must be updated to 400!
    assert findings_pass2[0]["diagnosed_status_code"] == 400
    assert findings_pass2[0]["diagnosed_failure_reason"] == "Status code mismatch: expected 200, got 400"




