from typing import Any, Dict, Optional
from app.agents.generators.base_generator import generate_category_test_cases
from app.llm.client import LLMClient


async def security_generator_node(state_or_payload: Dict[str, Any], llm_client: Optional[LLMClient] = None) -> Dict[str, Any]:
    """Security test generator node."""
    return await generate_category_test_cases(
        category="security",
        prompt_filename="security_v1.md",
        state_or_payload=state_or_payload,
        llm_client=llm_client
    )
