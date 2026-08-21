from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class TestCaseRequestModel(BaseModel):
    method: str = Field(..., description="HTTP Method (GET, POST, PUT, DELETE, etc.)")
    path: str = Field(..., description="API Path (e.g. /pet or /pet/123)")
    headers: Dict[str, str] = Field(default_factory=dict, description="Request headers")
    query_params: Dict[str, Any] = Field(default_factory=dict, description="Query parameters")
    path_params: Dict[str, Any] = Field(default_factory=dict, description="Path parameters")
    body: Optional[Any] = Field(default=None, description="Request body JSON payload or raw string")


class TestCaseExpectedModel(BaseModel):
    status_code: int = Field(..., description="Expected HTTP response status code (e.g. 200, 400, 401, 403, 404, 422, 500)")
    response_schema_match: bool = Field(default=True, description="Whether response body should match spec schema")
    error_code: Optional[str] = Field(default=None, description="Expected application error code if applicable")


class TestCaseLLMInputModel(BaseModel):
    """Pydantic schema passed to LLM for test case generation (excludes id to prevent collisions)."""
    endpoint_operation_id: str = Field(..., description="Target OpenAPI operation_id")
    category: Literal[
        "functional",
        "negative",
        "edge_case",
        "security",
        "auth",
        "authz",
        "regression",
        "performance"
    ] = Field(..., description="Test case category")
    name: str = Field(..., description="Short descriptive test case name")
    request: TestCaseRequestModel = Field(..., description="HTTP request details")
    expected: TestCaseExpectedModel = Field(..., description="Expected HTTP response criteria")
    priority: Literal["low", "medium", "high", "critical"] = Field("medium", description="Execution priority")
    generated_by: str = Field("generator_v1", description="Identifier of generator agent e.g. functional_generator_v1")
    rationale: str = Field("Generated per API spec schema rules", description="Detailed technical rationale for why this test case was generated")


class TestCaseLLMInputListModel(BaseModel):
    """Container list model for LLM structured output generation."""
    test_cases: List[TestCaseLLMInputModel] = Field(
        default_factory=list,
        description="List of generated test cases matching requested category and count"
    )


class TestCaseGeneratedModel(BaseModel):
    """Full TestCase model with globally unique id matching app/graph/state.py."""
    id: str = Field(..., description="Globally unique test case ID e.g. tc_functional_a1b2c3d4")
    endpoint_operation_id: str = Field(..., description="Target OpenAPI operation_id")
    category: Literal[
        "functional",
        "negative",
        "edge_case",
        "security",
        "auth",
        "authz",
        "regression",
        "performance"
    ] = Field(..., description="Test case category")
    name: str = Field(..., description="Short descriptive test case name")
    request: TestCaseRequestModel = Field(..., description="HTTP request details")
    expected: TestCaseExpectedModel = Field(..., description="Expected HTTP response criteria")
    priority: Literal["low", "medium", "high", "critical"] = Field("medium", description="Execution priority")
    generated_by: str = Field("generator_v1", description="Identifier of generator agent e.g. functional_generator_v1")
    rationale: str = Field("Generated per API spec schema rules", description="Detailed technical rationale for why this test case was generated")


class TestCaseListModel(BaseModel):
    """List model container for fully validated TestCaseGeneratedModel objects."""
    test_cases: List[TestCaseGeneratedModel] = Field(
        default_factory=list,
        description="List of fully constructed TestCase objects"
    )
