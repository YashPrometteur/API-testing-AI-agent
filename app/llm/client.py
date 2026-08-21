from typing import Any, List, Protocol, Type, TypeVar, runtime_checkable
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class LLMClient(Protocol):
    """Protocol for LLM clients supporting structured output generation and embeddings."""

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str = "",
        temperature: float = 0.2
    ) -> T:
        """Generate structured Pydantic response from LLM."""
        ...

    async def embed(self, text: str, **kwargs) -> List[float]:
        """Generate embedding vector for input text."""
        ...
