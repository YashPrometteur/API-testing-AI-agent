"""
Base module for LangGraph agent nodes.

RULE: agents/ folder has ZERO imports from api/ or repositories/.
Agents receive state as a function argument and return partial state updates only.
This keeps every agent unit-testable with a mocked LLMClient.
"""
from typing import Any, Dict
from app.graph.state import TestRunState


class BaseAgent:
    """Base class or interface helper for agents."""
    
    async def __call__(self, state: TestRunState) -> Dict[str, Any]:
        raise NotImplementedError
