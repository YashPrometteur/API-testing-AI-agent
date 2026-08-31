import asyncio
import json
import logging
import random
from typing import List, Type, TypeVar
from pydantic import BaseModel
from openai import AsyncOpenAI, APIError, RateLimitError, APIConnectionError

from app.config.settings import settings

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("openrouter_client")


class OpenRouterClient:
    """OpenRouter client implementation using OpenAI-compatible API format."""

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or settings.OPENROUTER_API_KEY
        self.model = model or settings.OPENROUTER_MODEL or "meta-llama/llama-3.3-70b-instruct"
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": "http://localhost:8001",
                "X-Title": "AI API Testing Agent",
            }
        ) if self.api_key else None

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str = "",
        temperature: float = 0.4
    ) -> T:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not configured in .env settings.")

        json_schema = response_model.model_json_schema()
        schema_instruction = (
            f"\n\nCRITICAL: You MUST respond ONLY with a valid JSON object strictly matching this JSON schema:\n"
            f"```json\n{json.dumps(json_schema, indent=2)}\n```\nDo not include any commentary, markdown wrapping, or raw text outside the JSON object."
        )
        full_system_prompt = (system_prompt or "You are a precise AI API Testing Agent.") + schema_instruction

        messages = [
            {"role": "system", "content": full_system_prompt},
            {"role": "user", "content": prompt}
        ]

        max_attempts = 3
        last_exception = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=temperature,
                    max_tokens=1500,
                )
                raw_text = response.choices[0].message.content or ""
                raw_text_clean = raw_text.strip()
                if raw_text_clean.startswith("```json"):
                    raw_text_clean = raw_text_clean[7:]
                if raw_text_clean.startswith("```"):
                    raw_text_clean = raw_text_clean[3:]
                if raw_text_clean.endswith("```"):
                    raw_text_clean = raw_text_clean[:-3]
                raw_text_clean = raw_text_clean.strip()

                return response_model.model_validate_json(raw_text_clean)
            except (RateLimitError, APIConnectionError, APIError) as e:
                last_exception = e
                if attempt < max_attempts:
                    delay = 2.0 ** attempt + random.uniform(0.2, 1.0)
                    logger.warning(f"[OPENROUTER_CLIENT] API error (attempt {attempt}/{max_attempts}): {e}. Retrying in {delay:.2f}s...")
                    await asyncio.sleep(delay)
                else:
                    raise e
            except Exception as e:
                raise e

        raise last_exception or RuntimeError("Failed to generate structured output from OpenRouter.")

    async def embed(self, text: str, **kwargs) -> List[float]:
        import hashlib
        dim = 384
        vector = [0.0] * dim
        words = text.lower().split()
        for w in words:
            h = int(hashlib.md5(w.encode('utf-8')).hexdigest(), 16)
            idx = h % dim
            val = 1.0 if (h % 2 == 0) else -1.0
            vector[idx] += val
        norm = sum(x * x for x in vector) ** 0.5
        if norm > 0:
            return [x / norm for x in vector]
        vector[0] = 1.0
        return vector
