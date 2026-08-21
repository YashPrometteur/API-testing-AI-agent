from pydantic import BaseModel, Field
from typing import List, Literal, Optional


class FindingModel(BaseModel):
    id: str
    test_case_id: str
    severity: Literal["low", "medium", "high", "critical"]
    confidence: float
    root_cause_summary: str
    suggested_fix: str
    supporting_evidence: List[str] = Field(default_factory=list)
    is_duplicate: bool = False
    guardrail_status: Literal["pending", "auto_approved", "pending_review", "approved", "rejected"] = "pending"
    diagnosed_status_code: Optional[int] = None
    diagnosed_failure_reason: Optional[str] = None
