from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


class EndpointModel(BaseModel):
    path: str
    method: str
    operation_id: str
    parameters: List[Dict[str, Any]] = Field(default_factory=list)
    request_schema: Optional[Dict[str, Any]] = None
    response_schemas: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    security_requirements: List[Dict[str, Any]] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
