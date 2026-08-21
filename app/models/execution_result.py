from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional, Literal


class ExecutionResultModel(BaseModel):
    test_case_id: str
    status: Literal["pass", "fail", "error", "flaky"]
    actual_status_code: Optional[int] = None
    actual_body: Optional[Any] = None
    actual_response: Optional[Any] = None
    latency_ms: float
    attempt_count: int = 1
    schema_valid: Optional[bool] = None
    assertion_failures: List[str] = Field(default_factory=list)
    executed_at: str
