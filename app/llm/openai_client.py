import asyncio
import logging
from typing import List, Type, TypeVar
try:
    from openai import AsyncOpenAI, APIError, RateLimitError, APIConnectionError
except ImportError:
    AsyncOpenAI = None
    APIError = Exception
    RateLimitError = Exception
    APIConnectionError = Exception

from app.config.settings import settings
from app.llm.client import LLMClient
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("openai_client")


class OpenAIClient:
    """OpenAI client implementation using Structured Outputs (schema-constrained JSON mode)."""

    def __init__(self, api_key: str = None, model: str = "gpt-4o"):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.model = model
        self.client = AsyncOpenAI(api_key=self.api_key) if self.api_key else None

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str = "",
        temperature: float = 0.2
    ) -> T:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured in settings or environment.")

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        delays = [1.0, 2.0]
        last_exception = None

        for attempt in range(3):
            try:
                response = await self.client.beta.chat.completions.parse(
                    model=self.model,
                    messages=messages,
                    response_format=response_model,
                    temperature=temperature,
                )
                parsed = response.choices[0].message.parsed
                if parsed is None:
                    raise ValueError("OpenAI returned null parsed structured output")
                return parsed
            except (RateLimitError, APIConnectionError, APIError) as e:
                last_exception = e
                if attempt < 2:
                    delay = delays[attempt]
                    logger.warning(f"OpenAI API transport error (attempt {attempt + 1}/3): {e}. Retrying in {delay}s...")
                    await asyncio.sleep(delay)
                else:
                    raise e
            except Exception as e:
                raise e

        raise last_exception or RuntimeError("Failed to generate structured output from OpenAI.")

    async def embed(self, text: str, **kwargs) -> List[float]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured in settings or environment.")
        
        response = await self.client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
        )
        return response.data[0].embedding
