import os
import pytest
from typing import Any, Dict, List, Type, TypeVar
from pydantic import BaseModel
from fastapi.testclient import TestClient
from motor.motor_asyncio import AsyncIOMotorClient

from app.main import app
from app.agents.discovery import DiscoveryAgent
from app.agents.planner import PlannerAgent
from app.config.settings import settings
from app.graph.build import build_graph
from app.llm.groq_client import GroqClient
from app.models.test_plan import EndpointPlanModel, TestPlan
from app.tools.spec_parser import OpenAPIValidatorTool, SpecParseError, SwaggerParserTool

T = TypeVar("T", bound=BaseModel)

PETSTORE_SWAGGER_2_0 = {
    "swagger": "2.0",
    "info": {"title": "Sample Petstore API", "version": "1.0.0"},
    "host": "petstore.swagger.io",
    "basePath": "/v2",
    "schemes": ["https"],
    "paths": {
        "/pet": {
            "post": {
                "operationId": "addPet",
                "summary": "Add a new pet to the store",
                "parameters": [
                    {
                        "in": "body",
                        "name": "body",
                        "required": True,
                        "schema": {"$ref": "#/definitions/Pet"}
                    }
                ],
                "responses": {"200": {"description": "Pet added"}},
                "security": [{"petstore_auth": ["write:pets", "read:pets"]}]
            }
        },
        "/pet/{petId}": {
            "get": {
                "operationId": "getPetById",
                "summary": "Find pet by ID",
                "parameters": [
                    {"name": "petId", "in": "path", "required": True, "type": "integer"}
                ],
                "responses": {"200": {"description": "successful operation"}}
            },
            "delete": {
                "operationId": "deletePet",
                "summary": "Deletes a pet",
                "parameters": [
                    {"name": "petId", "in": "path", "required": True, "type": "integer"}
                ],
                "responses": {"400": {"description": "Invalid ID supplied"}},
                "security": [{"petstore_auth": ["write:pets"]}]
            }
        }
    },
    "definitions": {
        "Pet": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "string"},
                "status": {"type": "string", "enum": ["available", "pending", "sold"]}
            }
        }
    },
    "securityDefinitions": {
        "petstore_auth": {
            "type": "oauth2",
            "authorizationUrl": "https://petstore.swagger.io/oauth/dialog",
            "flow": "implicit"
        }
    }
}


class MockPlannerLLMClient:
    """Mock LLMClient producing deterministic TestPlan for offline unit testing."""

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str = "",
        temperature: float = 0.2
    ) -> T:
        if response_model == TestPlan:
            plan_dict = {
                "addPet": EndpointPlanModel(
                    categories=["functional", "negative", "auth"],
                    target_case_count={"functional": 2, "negative": 2, "auth": 1},
                    priority="high"
                ),
                "getPetById": EndpointPlanModel(
                    categories=["functional"],
                    target_case_count={"functional": 1},
                    priority="medium"
                ),
                "deletePet": EndpointPlanModel(
                    categories=["functional", "negative", "auth", "authz"],
                    target_case_count={"functional": 1, "negative": 2, "auth": 1, "authz": 1},
                    priority="critical"
                )
            }
            return TestPlan(plan=plan_dict)
        
        from app.models.test_case import TestCaseExpectedModel, TestCaseLLMInputListModel, TestCaseLLMInputModel, TestCaseRequestModel
        tc = TestCaseLLMInputModel(
            endpoint_operation_id="addPet",
            category="functional",
            name="Mock Test Case",
            request=TestCaseRequestModel(method="POST", path="/pet", body={"name": "Doggie"}),
            expected=TestCaseExpectedModel(status_code=200, response_schema_match=True),
            priority="high",
            generated_by="mock_v1",
            rationale="Mock unit test"
        )
        return TestCaseLLMInputListModel(test_cases=[tc])

    async def embed(self, text: str) -> List[float]:
        return [0.0] * 1536


@pytest.mark.asyncio
async def test_swagger_parser_tool_conversion_and_ref_resolution():
    res = await SwaggerParserTool.parse(PETSTORE_SWAGGER_2_0)
    assert res.success is True
    normalized = res.data
    assert normalized.get("openapi") == "3.0.3"
    assert "servers" in normalized
    assert "components" in normalized
    assert "schemas" in normalized["components"]
    assert "Pet" in normalized["components"]["schemas"]
    assert res.metadata.get("endpoint_count") == 3

    val_res = OpenAPIValidatorTool.validate(normalized)
    assert val_res.data.get("valid") is True


@pytest.mark.asyncio
async def test_discovery_agent():
    discovery = DiscoveryAgent()
    state = {"spec_source": PETSTORE_SWAGGER_2_0, "status": "running"}
    out = await discovery(state)

    assert out.get("status") == "running"
    endpoints = out.get("endpoints", [])
    assert len(endpoints) == 3

    op_ids = [e["operation_id"] for e in endpoints]
    assert "addPet" in op_ids
    assert "getPetById" in op_ids
    assert "deletePet" in op_ids


@pytest.mark.asyncio
async def test_planner_agent_with_mock_llm():
    discovery = DiscoveryAgent()
    disc_out = await discovery({"spec_source": PETSTORE_SWAGGER_2_0, "status": "running"})
    endpoints = disc_out["endpoints"]

    mock_llm = MockPlannerLLMClient()
    planner = PlannerAgent(llm_client=mock_llm)

    plan_out = await planner({"endpoints": endpoints, "categories": ["functional", "negative", "auth", "authz"], "status": "running"})
    assert plan_out.get("status") == "running"
    test_plan = plan_out.get("test_plan", {})
    assert "plan" in test_plan
    assert "addPet" in test_plan["plan"]
    assert "deletePet" in test_plan["plan"]
    assert "authz" in test_plan["plan"]["deletePet"]["categories"]


@pytest.mark.asyncio
async def test_planner_agent_enforces_user_requested_categories():
    """Verify PlannerAgent enforces full user-requested categories even when LLM proposes a narrower subset."""
    discovery = DiscoveryAgent()
    disc_out = await discovery({"spec_source": PETSTORE_SWAGGER_2_0, "status": "running"})
    endpoints = disc_out["endpoints"]

    mock_llm = MockPlannerLLMClient()
    planner = PlannerAgent(llm_client=mock_llm)

    # User explicitly requests functional, negative, edge_case (mock LLM only proposes functional, negative for addPet)
    plan_out = await planner({
        "endpoints": endpoints,
        "categories": ["functional", "negative", "edge_case"],
        "status": "running"
    })
    assert plan_out.get("status") == "running"
    test_plan = plan_out.get("test_plan", {})
    plan = test_plan.get("plan", {})
    assert "addPet" in plan
    assert plan["addPet"]["categories"] == ["functional", "negative", "edge_case"]
    assert plan["addPet"]["target_case_count"]["edge_case"] == 1



@pytest.mark.asyncio
async def test_planner_agent_live_groq_or_skip():
    groq_key = settings.GROQ_API_KEY or os.getenv("GROQ_API_KEY")
    if not groq_key or "your_groq_api_key" in groq_key:
        pytest.skip("GROQ_API_KEY not configured for live LLM test.")

    discovery = DiscoveryAgent()
    disc_out = await discovery({"spec_source": PETSTORE_SWAGGER_2_0, "status": "running"})
    endpoints = disc_out["endpoints"]

    groq_client = GroqClient(api_key=groq_key)
    planner = PlannerAgent(llm_client=groq_client)

    try:
        plan_out = await planner({"endpoints": endpoints, "status": "running"})
    except Exception as e:
        if "429" in str(e) or "rate_limit" in str(e):
            pytest.skip(f"Skipping live Groq test due to rate limit/429 error: {e}")
        raise e

    if plan_out.get("status") == "failed":
        # Check if error message is a rate limit error
        err_msg = "".join(plan_out.get("errors", []))
        if "429" in err_msg or "rate_limit" in err_msg:
            pytest.skip(f"Skipping live Groq test because PlannerAgent failed due to rate limit/429: {err_msg}")

    assert plan_out.get("status") == "running"
    test_plan = plan_out.get("test_plan", {})
    assert "plan" in test_plan
    assert len(test_plan["plan"]) > 0


@pytest.mark.asyncio
async def test_specs_and_runs_api_endpoints(monkeypatch):
    client = TestClient(app)

    # 1. POST /api/v1/specs validation errors
    resp_422 = client.post("/api/v1/specs", json={})
    assert resp_422.status_code == 422

    from app.database import get_mongo_db
    db = await get_mongo_db()

    spec_id = "spec_test_petstore_001"
    await db["specs"].delete_one({"_id": spec_id})
    await db["specs"].insert_one({
        "_id": spec_id,
        "name": "Test Petstore Spec",
        "source_type": "dict",
        "source_ref": "petstore.json",
        "openapi_version": "3.0.3",
        "raw_spec": PETSTORE_SWAGGER_2_0,
        "endpoint_count": 3,
        "created_at": "2026-07-29T12:00:00Z",
        "updated_at": "2026-07-29T12:00:00Z",
        "is_deleted": False,
    })

    # 2. POST /api/v1/runs
    run_req = {
        "spec_id": spec_id,
        "target_base_url": "https://petstore.swagger.io/v2",
        "options": {"max_reflection_loops": 2, "min_confidence_threshold": 0.6}
    }

    # Patch get_llm_client in planner_agent and base_generator to use MockPlannerLLMClient for deterministic API test
    mock_llm = MockPlannerLLMClient()
    import app.agents.planner.planner_agent as planner_module
    import app.agents.generators.base_generator as base_gen_module

    monkeypatch.setattr(planner_module, "get_llm_client", lambda: mock_llm)
    monkeypatch.setattr(base_gen_module, "get_llm_client", lambda: mock_llm)

    run_resp = client.post("/api/v1/runs", json=run_req)
    assert run_resp.status_code == 202
    run_data = run_resp.json()
    assert "run_id" in run_data
    assert run_data["status"] in ["running", "completed"]

    # 3. GET /api/v1/runs/{run_id}
    run_id = run_data["run_id"]
    get_resp = client.get(f"/api/v1/runs/{run_id}")
    assert get_resp.status_code == 200
    fetched_run = get_resp.json()
    assert fetched_run["_id"] == run_id
    assert fetched_run["spec_id"] == spec_id

    # Clean up test data
    await db["specs"].delete_one({"_id": spec_id})
    await db["runs"].delete_one({"_id": run_id})
