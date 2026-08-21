from typing import Any, Dict, Optional
from app.agents.generators.base_generator import generate_category_test_cases
from app.llm.client import LLMClient


async def negative_generator_node(state_or_payload: Dict[str, Any], llm_client: Optional[LLMClient] = None) -> Dict[str, Any]:
    """Negative test case generator node."""
    return await generate_category_test_cases(
        category="negative",
        prompt_filename="negative_v1.md",
        state_or_payload=state_or_payload,
        llm_client=llm_client
    )
