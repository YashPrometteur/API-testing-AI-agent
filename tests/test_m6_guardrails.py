import pytest
import os
import shutil
import sys
import uuid
import asyncio
from fastapi.testclient import TestClient
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, ".")

from app.main import app
from app.config.settings import settings
from app.graph.build import build_graph
from app.graph.checkpointer import MongoCheckpointSaver
from app.agents.guardrail.guardrail_agent import guardrail_agent_node
from app.utils.datetime import current_utc_iso
from app.tools.rag_retriever import get_chroma_client

client = TestClient(app)


@pytest.fixture(autouse=True)
async def cleanup_db():
    from app.database import get_mongo_db
    db = await get_mongo_db()
    test_id_pattern = {"_id": {"$regex": "^(test_|mock_|run_conc|find_conc|def_conc|find_new_dup|def_existing)"}}
    await db["runs"].delete_many(test_id_pattern)
    await db["findings"].delete_many(test_id_pattern)
    await db["defects"].delete_many(test_id_pattern)

    # Clear vectors and wipe disk directory to ensure clean isolated collections
    chroma_client = get_chroma_client()
    try:
        chroma_client.delete_collection("defects")
    except Exception:
        pass

    db_path = "./chroma_db"
    if os.path.exists(db_path):
        shutil.rmtree(db_path, ignore_errors=True)

    yield db


@pytest.mark.asyncio
async def test_low_risk_auto_approval(cleanup_db):
    """Verify that low/medium severity findings are auto-approved and never pause the run."""
    db = cleanup_db
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    # Insert a dummy run
    await db["runs"].insert_one({
        "_id": run_id,
        "spec_id": "petstore_spec",
        "status": "running"
    })

    # Prepare low severity findings
    findings = [
        {
            "id": "find_low_1",
            "test_case_id": "tc_001",
            "severity": "low",
            "confidence": 0.8,
            "root_cause_summary": "Slightly high response latency",
            "suggested_fix": "Optimize DB indexing",
            "supporting_evidence": [],
            "is_duplicate": False,
            "guardrail_status": "pending"
        }
    ]

    # Invoke guardrail agent node
    state = {
        "run_id": run_id,
        "findings": findings,
        "test_cases": [{"id": "tc_001", "name": "Get Pet", "endpoint_operation_id": "getPet"}]
    }

    res = await guardrail_agent_node(state)
    print("RES:", res)
    all_defects = await db["defects"].find({}).to_list(length=10)
    print("ALL DEFECTS IN DB:", all_defects)
    assert res["status"] == "completed"
    assert len(res["pending_human_review"]) == 0

    # Assert defect was persisted immediately in MongoDB
    defect = await db["defects"].find_one({"finding_id": "find_low_1"})
    assert defect is not None
    assert defect["severity"] == "low"
    assert defect["status"] == "open"


@pytest.mark.asyncio
async def test_critical_risk_human_review_interrupt(cleanup_db):
    """Verify that critical/high severity findings set status to awaiting_human and trigger interrupt."""
    db = cleanup_db
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    # Insert a dummy run
    await db["runs"].insert_one({
        "_id": run_id,
        "spec_id": "petstore_spec",
        "status": "running"
    })

    findings = [
        {
            "id": "find_crit_1",
            "test_case_id": "tc_002",
            "severity": "critical",
            "confidence": 0.95,
            "root_cause_summary": "SQL Injection vulnerability on endpoint",
            "suggested_fix": "Use prepared statements",
            "supporting_evidence": [],
            "is_duplicate": False,
            "guardrail_status": "pending"
        }
    ]

    # Invoke guardrail agent node
    state = {
        "run_id": run_id,
        "findings": findings,
        "test_cases": [{"id": "tc_002", "name": "SQL Injection Test", "endpoint_operation_id": "queryPet"}]
    }

    res = await guardrail_agent_node(state)
    assert res["status"] == "awaiting_human"
    assert "find_crit_1" in res["pending_human_review"]

    # Verify no defect is created in MongoDB yet
    defect = await db["defects"].find_one({"finding_id": "find_crit_1"})
    assert defect is None

    # Verify finding is saved in findings collection as 'pending_review'
    saved_finding = await db["findings"].find_one({"_id": "find_crit_1"})
    assert saved_finding is not None
    assert saved_finding["guardrail_status"] == "pending_review"


@pytest.mark.asyncio
async def test_semantic_deduplication(cleanup_db):
    """Verify that semantic similarity matching marks duplicate findings and skips defect creation."""
    db = cleanup_db
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    # Seed an existing defect
    await db["defects"].insert_one({
        "_id": "def_existing_1",
        "run_id": "run_old",
        "test_case_id": "tc_old",
        "finding_id": "find_old",
        "severity": "medium",
        "confidence": 0.9,
        "status": "open",
        "title": "Vulnerability",
        "root_cause_summary": "Vulnerability: SQL Injection on pet query parameter.",
        "suggested_fix": "Fix parameter parsing",
        "supporting_evidence": [],
        "created_at": current_utc_iso()
    })

    # Index the old defect in Chroma defects collection
    chroma_client = get_chroma_client()
    col = chroma_client.get_or_create_collection("defects")
    # Generates a mock vector for existing defect
    from app.tools.embeddings import EmbeddingTool
    embed_res = await EmbeddingTool.embed("Vulnerability: SQL Injection on pet query parameter.")
    assert embed_res.success
    col.upsert(
        ids=["def_existing_1"],
        documents=["Vulnerability: SQL Injection on pet query parameter."],
        embeddings=[embed_res.data]
    )

    # Prepare a finding containing the same root cause summary (will trigger semantic match)
    findings = [
        {
            "id": "find_new_dup",
            "test_case_id": "tc_new",
            "severity": "medium",
            "confidence": 0.9,
            "root_cause_summary": "Vulnerability: SQL Injection on pet query parameter.",
            "suggested_fix": "Fix parameter parsing",
            "supporting_evidence": [],
            "is_duplicate": False,
            "guardrail_status": "pending"
        }
    ]

    state = {
        "run_id": run_id,
        "findings": findings,
        "test_cases": [{"id": "tc_new", "name": "Dup Injection Test", "endpoint_operation_id": "queryPet"}]
    }

    res = await guardrail_agent_node(state)
    assert res["status"] == "completed"

    # Verify new finding is stored as rejected/duplicate
    saved_finding = await db["findings"].find_one({"_id": "find_new_dup"})
    assert saved_finding is not None
    assert saved_finding["is_duplicate"] is True
    assert saved_finding["guardrail_status"] == "rejected"
    assert saved_finding["duplicate_of"] == "def_existing_1"

    # Verify no new defect was created in defects collection
    new_defect = await db["defects"].find_one({"finding_id": "find_new_dup"})
    assert new_defect is None


@pytest.mark.asyncio
async def test_concurrent_reviews_and_resumption(cleanup_db):
    """Verify concurrent pending reviews handling: only the final decision resumes execution."""
    db = cleanup_db
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    # Setup 1. Insert run in MongoDB
    await db["runs"].insert_one({
        "_id": run_id,
        "spec_id": "petstore_spec",
        "status": "awaiting_human",
        "created_at": current_utc_iso()
    })

    # Setup 2. Insert two findings in pending_review state
    await db["findings"].insert_many([
        {
            "_id": "find_conc_1",
            "run_id": run_id,
            "test_case_id": "tc_1",
            "severity": "critical",
            "confidence": 0.9,
            "root_cause_summary": "Failure 1",
            "suggested_fix": "Fix 1",
            "supporting_evidence": [],
            "guardrail_status": "pending_review"
        },
        {
            "_id": "find_conc_2",
            "run_id": run_id,
            "test_case_id": "tc_2",
            "severity": "high",
            "confidence": 0.85,
            "root_cause_summary": "Failure 2",
            "suggested_fix": "Fix 2",
            "supporting_evidence": [],
            "guardrail_status": "pending_review"
        }
    ])

    # Setup 3. Save a mock checkpoint state for graph resumption
    checkpointer = MongoCheckpointSaver(db=db)
    graph = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": run_id}}
    initial_state = {
        "run_id": run_id,
        "findings": [
            {"id": "find_conc_1", "test_case_id": "tc_1", "severity": "critical", "confidence": 0.9, "root_cause_summary": "Failure 1", "suggested_fix": "Fix 1", "supporting_evidence": [], "guardrail_status": "pending_review", "is_duplicate": False},
            {"id": "find_conc_2", "test_case_id": "tc_2", "severity": "high", "confidence": 0.85, "root_cause_summary": "Failure 2", "suggested_fix": "Fix 2", "supporting_evidence": [], "guardrail_status": "pending_review", "is_duplicate": False}
        ],
        "test_cases": [
            {"id": "tc_1", "name": "TC1", "endpoint_operation_id": "op1", "category": "functional", "request": {"method": "GET", "path": "/op1"}},
            {"id": "tc_2", "name": "TC2", "endpoint_operation_id": "op2", "category": "negative", "request": {"method": "GET", "path": "/op2"}}
        ],
        "execution_results": [
            {"test_case_id": "tc_1", "status": "fail", "actual_status_code": 500, "latency_ms": 10.0, "attempt_count": 1, "schema_valid": False, "assertion_failures": ["Server error"], "executed_at": "2026-08-18T00:00:00Z"},
            {"test_case_id": "tc_2", "status": "fail", "actual_status_code": 404, "latency_ms": 12.0, "attempt_count": 1, "schema_valid": True, "assertion_failures": ["Status mismatch"], "executed_at": "2026-08-18T00:00:00Z"}
        ],
        "status": "awaiting_human",
        "pending_human_review": ["find_conc_1", "find_conc_2"]
    }
    # Invoke guardrail_interrupt node to pause graph at interrupt()
    try:
        await graph.ainvoke(initial_state, config=config)
    except Exception:
        pass

    # Call review API for first finding
    payload_1 = {"decision": "approve", "reviewer": "qa_tester_1", "notes": "Approved finding 1"}
    response_1 = client.patch(f"/api/v1/defects/find_conc_1/review", json=payload_1)
    assert response_1.status_code == 200
    res_json_1 = response_1.json()
    assert res_json_1["run_resumed"] is False  # Second finding is still pending!

    # Verify run remains in "awaiting_human" in Mongo
    run_doc_1 = client.get(f"/api/v1/runs/{run_id}").json()
    assert run_doc_1["status"] == "awaiting_human"
    assert run_doc_1["pending_review_count"] == 1

    # Call review API for second (last) finding
    payload_2 = {"decision": "reject", "reviewer": "qa_tester_1", "notes": "Rejected finding 2"}
    response_2 = client.patch(f"/api/v1/defects/find_conc_2/review", json=payload_2)
    assert response_2.status_code == 200
    res_json_2 = response_2.json()
    assert res_json_2["run_resumed"] is True  # All reviews completed, graph resumed!

    # Verify run is marked completed in Mongo and populated with report and test_case_executions
    run_doc_2 = client.get(f"/api/v1/runs/{run_id}").json()
    assert run_doc_2["status"] == "completed"
    assert "test_case_executions" in run_doc_2
    assert len(run_doc_2["test_case_executions"]) == 2
    assert run_doc_2["final_report"] is not None
    assert run_doc_2["final_report"]["summary"]["total_executions"] == 2

    # Assert findings in final_report reflect latest DB approval statuses
    report_findings = {f["id"]: f for f in run_doc_2["final_report"]["findings"]}
    assert report_findings["find_conc_1"]["guardrail_status"] == "approved"
    assert report_findings["find_conc_2"]["guardrail_status"] == "rejected"

    # Assert defects_created contains real def_ ID and excludes rejected finding
    defects_created = run_doc_2["final_report"]["summary"]["defects_created"]
    assert len(defects_created) == 1
    assert defects_created[0].startswith("def_")
    assert "find_conc_1" not in defects_created
    assert "find_conc_2" not in defects_created

