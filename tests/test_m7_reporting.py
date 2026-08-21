import pytest
import sys
import uuid
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, ".")

from app.main import app
from app.config.settings import settings
from app.agents.reporting.reporting_agent import reporting_agent_node, ReportSummary
from app.utils.datetime import current_utc_iso

client = TestClient(app)


@pytest.fixture(autouse=True)
async def cleanup_db():
    from app.database import get_mongo_db
    db = await get_mongo_db()
    test_id_pattern = {"_id": {"$regex": "^(test_|mock_|run_test_|find_test_|def_test_)"}}
    await db["runs"].delete_many(test_id_pattern)
    await db["reports"].delete_many(test_id_pattern)
    await db["defects"].delete_many(test_id_pattern)
    yield db


@pytest.mark.asyncio
async def test_reporting_agent_node_aggregation(cleanup_db):
    """Verify that reporting_agent_node aggregates counts and persists HTML/PDF properly."""
    db = cleanup_db
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    # Setup run doc
    await db["runs"].insert_one({
        "_id": run_id,
        "spec_id": "petstore_spec",
        "status": "running"
    })

    # Prepare mock execution results
    execution_results = [
        {"test_case_id": "tc_1", "status": "pass", "latency_ms": 10.0, "attempt_count": 1},
        {"test_case_id": "tc_2", "status": "fail", "latency_ms": 15.0, "attempt_count": 1},
        {"test_case_id": "tc_3", "status": "flaky", "latency_ms": 120.0, "attempt_count": 2},
    ]

    # Prepare findings
    findings = [
        {
            "id": "find_1",
            "test_case_id": "tc_2",
            "severity": "critical",
            "confidence": 0.9,
            "root_cause_summary": "Crit Summary",
            "suggested_fix": "Fix it",
            "supporting_evidence": [],
            "is_duplicate": False,
            "guardrail_status": "approved"
        }
    ]

    # Prepare state
    state = {
        "run_id": run_id,
        "execution_results": execution_results,
        "findings": findings,
        "test_cases": [
            {"id": "tc_1", "category": "functional", "endpoint_operation_id": "getPet", "name": "Get Pet TC"},
            {"id": "tc_2", "category": "negative", "endpoint_operation_id": "deletePet", "name": "Delete Pet TC"},
            {"id": "tc_3", "category": "functional", "endpoint_operation_id": "getPet", "name": "Get Pet Flaky"},
        ]
    }

    # Mock the LLM client call for summary paragraph
    mock_summary = ReportSummary(summary_text="Mocked executive narrative summary paragraph.")
    
    with patch("app.agents.reporting.reporting_agent.get_llm_client") as mock_get_llm:
        mock_client = AsyncMock()
        mock_client.generate_structured.return_value = mock_summary
        mock_get_llm.return_value = mock_client

        res = await reporting_agent_node(state)

        # Assert returned node state updates
        assert res["status"] == "completed"
        report = res["final_report"]
        assert report["run_id"] == run_id
        assert report["summary"]["total_executions"] == 3
        assert report["summary"]["pass_count"] == 1
        assert report["summary"]["fail_count"] == 1
        assert report["summary"]["flaky_count"] == 1
        assert report["summary"]["pass_rate"] == 1.0 / 3.0
        assert report["executive_summary"] == "Mocked executive narrative summary paragraph."
        assert "getPet" in report["summary"]["flaky_endpoints_detected"]

    # Verify run document was updated in Mongo
    run_doc = await db["runs"].find_one({"_id": run_id})
    assert run_doc["status"] == "completed"
    assert run_doc["final_report"] is not None

    # Verify report document HTML and PDF were stored in Mongo
    report_doc = await db["reports"].find_one({"_id": run_id})
    assert report_doc is not None
    assert "API Test Execution Report" in report_doc["html_content"]
    assert report_doc["pdf_content"] is not None


@pytest.mark.asyncio
async def test_reporting_llm_retry_fallback(cleanup_db):
    """Verify that reporting_agent_node retries 1x and falls back to empty summary on LLM failures."""
    db = cleanup_db
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    await db["runs"].insert_one({
        "_id": run_id,
        "spec_id": "petstore_spec",
        "status": "running"
    })

    state = {
        "run_id": run_id,
        "execution_results": [{"test_case_id": "tc_1", "status": "pass"}],
        "findings": [],
        "test_cases": [{"id": "tc_1", "category": "functional", "endpoint_operation_id": "getPet"}]
    }

    with patch("app.agents.reporting.reporting_agent.get_llm_client") as mock_get_llm:
        mock_client = AsyncMock()
        # Raise exceptions to trigger retries and fallback
        mock_client.generate_structured.side_effect = RuntimeError("OpenAI error")
        mock_get_llm.return_value = mock_client

        res = await reporting_agent_node(state)

        # Node should succeed despite LLM failure
        assert res["status"] == "completed"
        assert res["final_report"]["executive_summary"] == ""
        # generate_structured should have been called exactly 2 times (attempt 1 + 1 retry)
        assert mock_client.generate_structured.call_count == 2


@pytest.mark.asyncio
async def test_report_api_endpoints_formats(cleanup_db):
    """Verify GET /report returns 404, 409, and renders correct json/html/pdf formats."""
    db = cleanup_db
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    # Test 404
    resp = client.get(f"/api/v1/runs/{run_id}/report")
    assert resp.status_code == 404

    # Test 409 when still running
    await db["runs"].insert_one({
        "_id": run_id,
        "spec_id": "petstore_spec",
        "status": "running"
    })
    resp = client.get(f"/api/v1/runs/{run_id}/report")
    assert resp.status_code == 409

    # Setup completed report
    final_report = {
        "run_id": run_id,
        "status": "completed",
        "summary": {"total_executions": 1, "pass_count": 1, "fail_count": 0, "flaky_count": 0, "pass_rate": 1.0, "categories": {}},
        "executive_summary": "Test Summary Text",
        "findings": []
    }
    await db["runs"].update_one({"_id": run_id}, {"$set": {"status": "completed", "final_report": final_report}})
    await db["reports"].insert_one({
        "_id": run_id,
        "run_id": run_id,
        "html_content": "<html>Mock HTML</html>",
        "pdf_content": b"Mock PDF bytes"
    })

    # Query formats
    # JSON
    resp_json = client.get(f"/api/v1/runs/{run_id}/report")
    assert resp_json.status_code == 200
    assert resp_json.json()["executive_summary"] == "Test Summary Text"

    # HTML
    resp_html = client.get(f"/api/v1/runs/{run_id}/report?format=html")
    assert resp_html.status_code == 200
    assert resp_html.headers["content-type"] == "text/html; charset=utf-8"
    assert resp_html.text == "<html>Mock HTML</html>"

    # PDF
    resp_pdf = client.get(f"/api/v1/runs/{run_id}/report?format=pdf")
    assert resp_pdf.status_code == 200
    assert resp_pdf.headers["content-type"] == "application/pdf"
    assert resp_pdf.content == b"Mock PDF bytes"


@pytest.mark.asyncio
async def test_execution_history_trends_and_flakiness(cleanup_db):
    """Verify GET /history calculates correct flake rates and pass rate trends over multiple runs."""
    db = cleanup_db
    uid = uuid.uuid4().hex[:8]
    run_1_id = f"test_run_01_{uid}"
    run_2_id = f"test_run_02_{uid}"
    run_3_id = f"test_run_03_{uid}"
    spec_id = f"flaky_spec_{uid}"

    # Seed run documents
    await db["runs"].insert_many([
        {"_id": run_1_id, "spec_id": spec_id, "status": "completed"},
        {"_id": run_2_id, "spec_id": spec_id, "status": "completed"},
        {"_id": run_3_id, "spec_id": spec_id, "status": "completed"}
    ])

    # Run 1: 1 pass, 0 flaky for getPet (100% pass)
    state1 = {
        "run_id": run_1_id,
        "execution_results": [{"test_case_id": "tc1", "status": "pass"}],
        "findings": [],
        "test_cases": [{"id": "tc1", "category": "functional", "endpoint_operation_id": "getPet"}]
    }

    # Run 2: 1 flaky for getPet (flaky)
    state2 = {
        "run_id": run_2_id,
        "execution_results": [{"test_case_id": "tc1", "status": "flaky"}],
        "findings": [],
        "test_cases": [{"id": "tc1", "category": "functional", "endpoint_operation_id": "getPet"}]
    }

    # Run 3: 1 pass, 1 flaky for getPet (flaky)
    state3 = {
        "run_id": run_3_id,
        "execution_results": [
            {"test_case_id": "tc1", "status": "pass"},
            {"test_case_id": "tc2", "status": "flaky"}
        ],
        "findings": [],
        "test_cases": [
            {"id": "tc1", "category": "functional", "endpoint_operation_id": "getPet"},
            {"id": "tc2", "category": "functional", "endpoint_operation_id": "getPet"}
        ]
    }

    # Run reporting node sequentially for all three states to build database history
    with patch("app.agents.reporting.reporting_agent.get_llm_client") as mock_get_llm:
        mock_client = AsyncMock()
        mock_client.generate_structured.return_value = ReportSummary(summary_text="OK")
        mock_get_llm.return_value = mock_client

        await reporting_agent_node(state1)
        await reporting_agent_node(state2)
        await reporting_agent_node(state3)

    # Call history API
    resp = client.get(f"/api/v1/runs/{run_3_id}/history")
    assert resp.status_code == 200
    res_data = resp.json()

    assert res_data["spec_id"] == spec_id
    
    # Assert flaky endpoints details
    flaky_list = res_data["flaky_endpoints"]
    assert len(flaky_list) == 1
    assert flaky_list[0]["endpoint_operation_id"] == "getPet"
    # Flake rate: Run1 has 0 flaky, Run2 has 1 flaky, Run3 has 1 flaky out of 2.
    # Total executions across 3 runs: 1 + 1 + 2 = 4 executions.
    # Total flaky count: 0 + 1 + 1 = 2 flaky.
    # Flake rate = 2 / 4 = 50% (0.5)
    assert flaky_list[0]["flake_rate_last_10_runs"] == 0.5
    assert flaky_list[0]["last_flaky_at"] is not None

    # Assert pass rate trend (Run 1 = 100%, Run 2 = 0%, Run 3 = 50%)
    trend = res_data["pass_rate_trend"]
    assert len(trend) == 3
    assert trend[0]["run_id"] == run_1_id
    assert trend[0]["pass_rate"] == 1.0
    
    assert trend[1]["run_id"] == run_2_id
    assert trend[1]["pass_rate"] == 0.0

    assert trend[2]["run_id"] == run_3_id
    assert trend[2]["pass_rate"] == 0.5


@pytest.mark.asyncio
async def test_reporting_refreshes_findings_and_defects_from_db(cleanup_db):
    """Verify reporting_agent_node re-fetches authoritative guardrail_status and real def_ IDs from DB."""
    db = cleanup_db
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    # Seed run doc
    await db["runs"].insert_one({
        "_id": run_id,
        "spec_id": "test_spec",
        "status": "awaiting_human"
    })

    find_app_id = f"find_app_{uuid.uuid4().hex[:8]}"
    find_rej_id = f"find_rej_{uuid.uuid4().hex[:8]}"
    def_id = f"def_{uuid.uuid4().hex[:8]}"

    # Seed 2 findings in DB (1 approved, 1 rejected)
    await db["findings"].insert_many([
        {
            "_id": find_app_id,
            "run_id": run_id,
            "test_case_id": "tc_1",
            "severity": "high",
            "confidence": 0.9,
            "root_cause_summary": "Summary 1",
            "suggested_fix": "Fix 1",
            "supporting_evidence": [],
            "guardrail_status": "approved",
            "reviewer": "qa_lead",
            "review_notes": "Looks good"
        },
        {
            "_id": find_rej_id,
            "run_id": run_id,
            "test_case_id": "tc_2",
            "severity": "high",
            "confidence": 0.85,
            "root_cause_summary": "Summary 2",
            "suggested_fix": "Fix 2",
            "supporting_evidence": [],
            "guardrail_status": "rejected",
            "reviewer": "qa_lead",
            "review_notes": "False positive"
        }
    ])

    # Seed 1 approved defect in DB with real def_ ID
    await db["defects"].insert_one({
        "_id": def_id,
        "run_id": run_id,
        "spec_id": "test_spec",
        "test_case_id": "tc_1",
        "finding_id": find_app_id,
        "severity": "high",
        "confidence": 0.9,
        "status": "open",
        "title": "Defect Title",
        "root_cause_summary": "Summary 1",
        "suggested_fix": "Fix 1",
        "supporting_evidence": [],
        "created_at": current_utc_iso()
    })

    # In-graph state has stale "pending_review" status
    stale_findings = [
        {
            "id": find_app_id,
            "test_case_id": "tc_1",
            "severity": "high",
            "confidence": 0.9,
            "root_cause_summary": "Summary 1",
            "suggested_fix": "Fix 1",
            "supporting_evidence": [],
            "guardrail_status": "pending_review",
            "is_duplicate": False
        },
        {
            "id": find_rej_id,
            "test_case_id": "tc_2",
            "severity": "high",
            "confidence": 0.85,
            "root_cause_summary": "Summary 2",
            "suggested_fix": "Fix 2",
            "supporting_evidence": [],
            "guardrail_status": "pending_review",
            "is_duplicate": False
        }
    ]

    state = {
        "run_id": run_id,
        "execution_results": [
            {"test_case_id": "tc_1", "status": "fail", "latency_ms": 20.0, "attempt_count": 1},
            {"test_case_id": "tc_2", "status": "fail", "latency_ms": 25.0, "attempt_count": 1}
        ],
        "findings": stale_findings,
        "test_cases": [
            {"id": "tc_1", "category": "functional", "endpoint_operation_id": "op1", "name": "TC 1"},
            {"id": "tc_2", "category": "negative", "endpoint_operation_id": "op2", "name": "TC 2"}
        ]
    }

    with patch("app.agents.reporting.reporting_agent.get_llm_client") as mock_get_llm:
        mock_client = AsyncMock()
        mock_client.generate_structured.return_value = ReportSummary(summary_text="Report summary.")
        mock_get_llm.return_value = mock_client

        res = await reporting_agent_node(state)

        report = res["final_report"]
        assert report is not None
        
        # 1. Assert findings in final_report refreshed to DB values
        findings_by_id = {f["id"]: f for f in report["findings"]}
        assert findings_by_id[find_app_id]["guardrail_status"] == "approved"
        assert findings_by_id[find_app_id]["reviewer"] == "qa_lead"
        assert findings_by_id[find_rej_id]["guardrail_status"] == "rejected"
        assert findings_by_id[find_rej_id]["reviewer"] == "qa_lead"

        # 2. Assert defects_created contains real def_ ID and does NOT contain raw finding IDs
        defects_created = report["summary"]["defects_created"]
        assert defects_created == [def_id]
        assert find_app_id not in defects_created
        assert find_rej_id not in defects_created


