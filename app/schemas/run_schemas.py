from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List


class CreateRunRequest(BaseModel):
    spec_url: Optional[str] = None
    spec_json: Optional[Dict[str, Any]] = None
    target_base_url: str


class RunResponse(BaseModel):
    run_id: str
    status: str
    created_at: str
