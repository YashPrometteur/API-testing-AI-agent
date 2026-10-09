import asyncio
import json
import logging
import pytest
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
from groq import RateLimitError as GroqRateLimitError
from openai import RateLimitError as OpenAIRateLimitError

sys.path.insert(0, ".")

from app.main import app
from app.config.settings import settings
from app.tools.spec_parser import SwaggerParserTool, SpecParseError
from app.llm.structured_output import call_with_validation, StructuredOutputFailure
from app.llm.groq_client import GroqClient
from app.llm.openai_client import OpenAIClient
from app.middleware.error_handler import RunNotFoundError
from app.middleware.logging_middleware import JSONFormatter, instrument_node, ctx_run_id, ctx_node, ctx_correlation_id
from app.graph.build import build_graph
from app.graph.checkpointer import MongoCheckpointSaver
from app.graph.state import TestCase
from app.agents.reporting.reporting_agent import ReportSummary
from app.utils.datetime import current_utc_iso

client = TestClient(app, raise_server_exceptions=False)


# Dummy model for validation
class DummyResponse(BaseModel):
    message: str


@pytest.fixture(autouse=True)
async def cleanup_db():
    from app.database import get_mongo_db
    db = await get_mongo_db()
    test_id_pattern = {"_id": {"$regex": "^(test_|mock_|run_test_|find_test_|def_test_)"}}
    await db["runs"].delete_many(test_id_pattern)
    await db["reports"].delete_many(test_id_pattern)
    await db["defects"].delete_many(test_id_pattern)
    yield db


# --- 1. Audit Retry Paths ---

@pytest.mark.asyncio
async def test_spec_url_fetch_retry_limit():
    """Verify fetch_spec_url fails after exactly 3 attempts."""
    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.side_effect = Exception("Connection Refused")
        with pytest.raises(Exception):
            await SwaggerParserTool.fetch_spec_url("http://invalidurl/spec.json")
        assert mock_get.call_count == 3


@pytest.mark.asyncio
async def test_structured_output_re_prompt():
    """Verify call_with_validation re-prompts exactly once on validation failure."""
    mock_llm = AsyncMock()
    # First attempt raises ValidationError
    # Second attempt succeeds
    from pydantic import ValidationError
    from pydantic_core import InitErrorDetails, PydanticCustomError

    # Helper to construct a ValidationError dynamically
    def make_validation_error():
        try:
            class Dummy(BaseModel):
                val: int
            Dummy(val="not_an_int")
        except ValidationError as e:
            return e

    mock_llm.generate_structured.side_effect = [
        make_validation_error(),
        DummyResponse(message="Success after re-prompt")
    ]

    res = await call_with_validation(
        client=mock_llm,
        prompt="Ask user details",
        response_model=DummyResponse
    )
    assert res.message == "Success after re-prompt"
    assert mock_llm.generate_structured.call_count == 2


@pytest.mark.asyncio
async def test_llm_clients_transport_failure_retry_limit():
    """Verify Groq and OpenAI clients raise exceptions after exactly 3 failed transport attempts."""
    mock_request = MagicMock()
    mock_request.url = "https://api.openai.com/v1"
    mock_response = MagicMock()
    mock_response.headers = {}
    mock_response.status_code = 429
    
    openai_rate_limit_err = OpenAIRateLimitError("Rate limited", response=mock_response, body=None)
    groq_rate_limit_err = GroqRateLimitError("Rate limited", response=mock_response, body=None)

    # 1. Groq Client audit
    groq_client = GroqClient(api_key="mock_key")
    with patch.object(groq_client, "client") as mock_groq_sdk, \
         patch("app.llm.ollama_client.OllamaClient.generate_structured", side_effect=Exception("Ollama offline")), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_groq_sdk.chat.completions.create.side_effect = groq_rate_limit_err
        with pytest.raises(GroqRateLimitError):
            await groq_client.generate_structured(
                prompt="Prompt",
                response_model=DummyResponse
            )
        assert mock_groq_sdk.chat.completions.create.call_count >= 3

    # 2. OpenAI Client audit
    openai_client = OpenAIClient(api_key="mock_key")
    with patch.object(openai_client, "client") as mock_openai_sdk, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_openai_sdk.beta.chat.completions.parse.side_effect = openai_rate_limit_err
        with pytest.raises(OpenAIRateLimitError):
            await openai_client.generate_structured(
                prompt="Prompt",
                response_model=DummyResponse
            )
        assert mock_openai_sdk.beta.chat.completions.parse.call_count == 3


# --- 2. Middleware Exceptions Mapping ---

def test_global_exception_handler_spec_parse_error():
    """Verify SpecParseError maps to 400 Bad Request with correlation_id."""
    @app.get("/test-spec-error")
    async def trigger_spec_error():
        raise SpecParseError("Malformed swagger structure.")

    resp = client.get("/test-spec-error")
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "SpecParseError"
    assert "correlation_id" in data


def test_global_exception_handler_run_not_found_error():
    """Verify RunNotFoundError maps to 404 Not Found with correlation_id."""
    @app.get("/test-run-not-found")
    async def trigger_run_not_found():
        raise RunNotFoundError("Run run_123 not found in DB.")

    resp = client.get("/test-run-not-found")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "RunNotFoundError"
    assert "correlation_id" in data


def test_global_exception_handler_unhandled_error():
    """Verify unhandled Exception maps to 500 with correlation_id in body."""
    @app.get("/test-unhandled-error")
    async def trigger_unhandled():
        raise ValueError("Critical database connection dropped.")

    resp = client.get("/test-unhandled-error")
    assert resp.status_code == 500
    data = resp.json()
    assert data["error"] == "Internal Server Error"
    assert "correlation_id" in data


# --- 3. JSON Formatted Logging Audit ---

def test_json_formatted_logger(caplog):
    """Verify that JSONFormatter formats all log outputs as structured JSON lines."""
    formatter = JSONFormatter()
    logger = logging.getLogger("test_json_logger")
    
    # Setup log records
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Audited node entry point logic.",
        args=(),
        exc_info=None
    )
    
    # Inject context values
    ctx_run_id.set("run_abc123")
    ctx_node.set("discovery")
    ctx_correlation_id.set("corr_xyz")

    formatted_line = formatter.format(record)
    log_json = json.loads(formatted_line)

    assert log_json["level"] == "INFO"
    assert log_json["run_id"] == "run_abc123"
    assert log_json["node"] == "discovery"
    assert log_json["correlation_id"] == "corr_xyz"
    assert log_json["message"] == "Audited node entry point logic."


# --- 4. Happy Path and HITL approval End-To-End Graph execution ---

@pytest.mark.asyncio
async def test_happy_path_e2e_graph_run(cleanup_db):
    """Verify that a Happy Path run executes discovery, planning, validation, and reporting agent nodes cleanly."""
    db = cleanup_db
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    # Setup spec and run
    await db["runs"].insert_one({
        "_id": run_id,
        "spec_id": "happy_spec",
        "status": "running"
    })

    # Pre-populate state cases
    endpoints = [
        {"operation_id": "getHealth", "path": "/health", "method": "GET", "parameters": [], "response_schemas": {"200": {}}}
    ]
    test_cases = [
        TestCase(id="tc1", endpoint_operation_id="getHealth", category="functional", name="Happy path TC", request={"method": "GET", "path": "/health"}, expected={"status_code": 200}, priority="low", generated_by="functional_v1", rationale="")
    ]

    # Pre-populate successful execution mock
    async def mock_execute(*args, **kwargs):
        return type("Result", (), {
            "success": True,
            "data": {"status_code": 200, "headers": {}, "body": {"status": "ok"}, "latency_ms": 10.0, "attempt_count": 1, "transient_error": False}
        })()

    with patch("app.tools.http_executor.HTTPExecutionTool.execute", mock_execute), \
         patch("app.graph.build.DiscoveryAgent", lambda *a, **k: lambda s: {}), \
         patch("app.graph.build.PlannerAgent", lambda *a, **k: lambda s: {}), \
         patch("app.graph.build.functional_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.negative_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.edge_case_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.security_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.auth_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.authz_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.regression_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.agents.reporting.reporting_agent.get_llm_client") as mock_get_llm:

        mock_llm_client = AsyncMock()
        mock_llm_client.generate_structured.return_value = ReportSummary(summary_text="Happy summary") if 'ReportSummary' in globals() else type("Summary", (), {"summary_text": "Happy summary"})()
        mock_get_llm.return_value = mock_llm_client

        # Create and execute graph
        graph = build_graph()
        config = {"configurable": {"thread_id": run_id}}
        
        initial_state = {
            "run_id": run_id,
            "endpoints": endpoints,
            "test_cases": test_cases,
            "status": "running"
        }

        # Run Happy path
        state_out = await graph.ainvoke(initial_state, config=config)

        # Assert completed status and output
        assert state_out["status"] == "completed"
        assert state_out["final_report"] is not None


@pytest.mark.asyncio
async def test_forced_failure_hitl_approval_e2e_graph_run(cleanup_db):
    """
    Verify the HITL interrupt/resume contract end-to-end:
      1. Graph reaches guardrail_interrupt node with a pending review → raises GraphInterrupt
      2. Run status is 'awaiting_human' in DB
      3. Finding doc exists with guardrail_status='pending_review'
      4. PATCH /defects/{id}/review → run_resumed=True
      5. Run status flips to 'completed' in DB
      6. Defect doc created in DB

    The test seeds findings + pending_human_review directly in state and mocks
    guardrail_agent_node to avoid relying on the full execution→RCA chain
    (which has correct but complex flaky-detection semantics that are covered
    in dedicated M6 tests).
    """
    db = cleanup_db
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    finding_id = f"find_{uuid.uuid4().hex[:8]}"

    # Insert run doc
    await db["runs"].insert_one({
        "_id": run_id,
        "spec_id": "petstore_spec",
        "status": "running"
    })

    # Insert finding doc as guardrail agent would (pending_review)
    from app.utils.datetime import current_utc_iso
    await db["findings"].insert_one({
        "_id": finding_id,
        "run_id": run_id,
        "test_case_id": "tc_fail",
        "severity": "critical",
        "confidence": 0.95,
        "root_cause_summary": "Critical Command Injection Vulnerability",
        "suggested_fix": "Strictly sanitize all path variables.",
        "supporting_evidence": ["DELETE /pet/1 HTTP 500"],
        "is_duplicate": False,
        "guardrail_status": "pending_review",
        "created_at": current_utc_iso()
    })

    # Mock guardrail_agent_node: sets status to awaiting_human with the finding pending
    async def mock_guardrail(state):
        # Update run status to awaiting_human
        from app.database import get_mongo_db
        from app.repositories.run_repository import RunRepository
        db_conn = await get_mongo_db()
        run_repo = RunRepository(db_conn)
        await run_repo.update_status(run_id, "awaiting_human", {"updated_at": current_utc_iso()})
        return {
            "status": "awaiting_human",
            "pending_human_review": [finding_id],
        }

    checkpointer = MongoCheckpointSaver(db=db)

    with patch("app.graph.build.DiscoveryAgent", lambda *a, **k: lambda s: {}), \
         patch("app.graph.build.PlannerAgent", lambda *a, **k: lambda s: {}), \
         patch("app.graph.build.functional_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.negative_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.edge_case_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.security_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.auth_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.authz_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.regression_generator_node", lambda s, *a, **k: {"test_cases": []}), \
         patch("app.graph.build.rca_node", lambda s: {}), \
         patch("app.graph.build.reflection_agent_node", lambda s: {"route_decision": "proceed"}), \
         patch("app.graph.build.guardrail_agent_node", mock_guardrail), \
         patch("app.agents.reporting.reporting_agent.get_llm_client") as mock_get_llm:

        mock_llm_client = AsyncMock()
        mock_llm_client.generate_structured.return_value = ReportSummary(summary_text="E2E Failure Summary")
        mock_get_llm.return_value = mock_llm_client

        # Build graph with checkpointer for interrupt support
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": run_id}}

        test_cases = [
            TestCase(
                id="tc_fail", endpoint_operation_id="deletePet",
                category="functional", name="Delete Pet TC",
                request={"method": "DELETE", "path": "/pet/1"},
                expected={"status_code": 200}, priority="high",
                generated_by="functional_v1", rationale=""
            )
        ]
        execution_results = [{
            "test_case_id": "tc_fail",
            "status": "fail",
            "actual_status_code": 500,
            "actual_body": "Internal Error",
            "latency_ms": 150.0,
            "attempt_count": 1,
            "schema_valid": False,
            "assertion_failures": ["Expected 200 got 500"],
            "executed_at": current_utc_iso()
        }]
        findings = [{
            "id": finding_id,
            "test_case_id": "tc_fail",
            "severity": "critical",
            "confidence": 0.95,
            "root_cause_summary": "Critical Command Injection Vulnerability",
            "suggested_fix": "Strictly sanitize all path variables.",
            "supporting_evidence": ["DELETE /pet/1 HTTP 500"],
            "is_duplicate": False,
            "guardrail_status": "pending_review"
        }]

        initial_state = {
            "run_id": run_id,
            "endpoints": [{"operation_id": "deletePet", "path": "/pet/{petId}", "method": "DELETE", "parameters": [], "response_schemas": {"200": {}}}],
            "test_cases": test_cases,
            "execution_results": execution_results,
            "findings": findings,
            "status": "running",
            "reflection_count": 2,  # Skip reflection by max-count
            "route_decision": "proceed"
        }

        # ── Phase 1: Run graph → expect interrupt at guardrail_interrupt ──
        # LangGraph 1.2: with a checkpointer, ainvoke() does NOT raise GraphInterrupt.
        # Instead it returns a dict with "__interrupt__" key when the graph pauses.
        result = await graph.ainvoke(initial_state, config=config)
        assert "__interrupt__" in result, (
            f"Expected graph to pause at guardrail_interrupt but got no __interrupt__ key. "
            f"Result keys: {list(result.keys())}"
        )
        interrupts = result["__interrupt__"]
        assert len(interrupts) > 0, "Expected at least one interrupt value"
        assert interrupts[0].value.get("run_id") == run_id

        # Phase 1 assertions
        run_doc = await db["runs"].find_one({"_id": run_id})
        assert run_doc["status"] == "awaiting_human", \
            f"Expected awaiting_human, got {run_doc.get('status')}"

        finding_doc = await db["findings"].find_one({"_id": finding_id})
        assert finding_doc is not None
        assert finding_doc["guardrail_status"] == "pending_review"

        # ── Phase 2: Approve via PATCH API → graph resumes to completed ──
        payload = {"decision": "approve", "reviewer": "admin_auditor", "notes": "Approved."}
        resp = client.patch(f"/api/v1/defects/{finding_id}/review", json=payload)
        assert resp.status_code == 200, f"PATCH failed: {resp.text}"
        body = resp.json()
        assert body["run_resumed"] is True
        assert body["status"] == "approved"

        # Phase 2 assertions
        completed_run = await db["runs"].find_one({"_id": run_id})
        assert completed_run["status"] == "completed", \
            f"Expected completed, got {completed_run.get('status')}"

        defect_doc = await db["defects"].find_one({"finding_id": finding_id})
        assert defect_doc is not None
        assert defect_doc["status"] == "open"
        assert defect_doc["_id"].startswith("def_")
        assert "Defect in" in defect_doc["title"]

