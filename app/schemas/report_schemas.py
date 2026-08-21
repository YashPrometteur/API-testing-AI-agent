from pydantic import BaseModel
from typing import Dict, Any, List


class ReportDTO(BaseModel):
    run_id: str
    summary: Dict[str, Any]
    total_test_cases: int
    passed_cases: int
    failed_cases: int
