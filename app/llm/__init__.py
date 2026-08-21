from app.config.settings import settings
from app.llm.client import LLMClient
from app.llm.openai_client import OpenAIClient
from app.llm.groq_client import GroqClient
from app.llm.openrouter_client import OpenRouterClient


def get_llm_client() -> LLMClient:
    """Factory function: Returns GroqClient, OpenRouterClient, or OpenAIClient based on settings.LLM_PROVIDER."""
    provider = (settings.LLM_PROVIDER or "openai").lower()
    if provider == "groq":
        return GroqClient()
    elif provider == "openrouter":
        return OpenRouterClient()
    return OpenAIClient()
