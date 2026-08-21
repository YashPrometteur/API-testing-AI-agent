from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional


class ReportModel(BaseModel):
    run_id: str
    summary: Dict[str, Any] = Field(default_factory=dict)
    findings: List[Dict[str, Any]] = Field(default_factory=list)
    generated_at: str
