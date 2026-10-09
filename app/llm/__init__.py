import os
from app.config.settings import settings
from app.llm.client import LLMClient
from app.llm.openai_client import OpenAIClient
from app.llm.groq_client import GroqClient
from app.llm.openrouter_client import OpenRouterClient


def get_llm_client() -> LLMClient:
    """Factory function: Returns GroqClient, OpenRouterClient, or OpenAIClient based on LLM_PROVIDER."""
    provider = (os.getenv("LLM_PROVIDER") or settings.LLM_PROVIDER or "groq").lower()
    if provider == "groq":
        return GroqClient()
    elif provider == "ollama":
        from app.llm.ollama_client import OllamaClient
        return OllamaClient()
    elif provider == "openrouter":
        return OpenRouterClient()
    return OpenAIClient()

