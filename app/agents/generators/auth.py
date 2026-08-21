from typing import Any, Dict, Optional
from app.agents.generators.base_generator import generate_category_test_cases
from app.llm.client import LLMClient


async def auth_generator_node(state_or_payload: Dict[str, Any], llm_client: Optional[LLMClient] = None) -> Dict[str, Any]:
    """Auth test generator node."""
    return await generate_category_test_cases(
        category="auth",
        prompt_filename="auth_v1.md",
        state_or_payload=state_or_payload,
        llm_client=llm_client
    )
