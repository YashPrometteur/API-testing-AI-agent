from typing import Dict, List, Literal
from pydantic import BaseModel, Field


class EndpointPlanModel(BaseModel):
    categories: List[Literal[
        "functional",
        "negative",
        "edge_case",
        "security",
        "auth",
        "authz",
        "regression",
        "performance"
    ]] = Field(default_factory=list, description="Target test categories for this endpoint")
    target_case_count: Dict[str, int] = Field(
        default_factory=dict,
        description="Target number of test cases to generate per category"
    )
    priority: Literal["low", "medium", "high", "critical"] = Field(
        default="medium",
        description="Assigned test execution priority for this endpoint"
    )


class TestPlan(BaseModel):
    plan: Dict[str, EndpointPlanModel] = Field(
        default_factory=dict,
        description="Map of operation_id to EndpointPlanModel specifying test coverage strategy"
    )
