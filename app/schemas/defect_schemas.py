from pydantic import BaseModel
from typing import List, Optional


class DefectDTO(BaseModel):
    id: str
    run_id: str
    title: str
    severity: str
    status: str
    created_at: str
