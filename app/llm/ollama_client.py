import asyncio
import json
import logging
import re
from typing import List, Type, TypeVar
import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("ollama_client")


class OllamaClient:
    """Local Ollama client supporting structured output using local qwen2.5:7b."""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen2.5:7b"):
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str = "",
        temperature: float = 0.2
    ) -> T:
        json_schema = json.dumps(response_model.model_json_schema(), indent=2)
        schema_instruction = (
            f"\n\nCRITICAL: You MUST respond ONLY with a valid JSON object strictly matching this schema:\n"
            f"```json\n{json_schema}\n```\nDo not include any commentary, explanations, or text outside the JSON object."
        )
        full_system_prompt = (system_prompt or "You are a precise AI API Testing Agent.") + schema_instruction

        messages = [
            {"role": "system", "content": full_system_prompt},
            {"role": "user", "content": prompt}
        ]

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": temperature
            }
        }

        last_exception = None
        ollama_timeout = httpx.Timeout(connect=1.5, read=4.0, write=3.0, pool=1.5)
        for attempt in range(1, 2):
            try:
                async with httpx.AsyncClient(timeout=ollama_timeout) as client:
                    res = await client.post(f"{self.base_url}/api/chat", json=payload)

                if res.status_code != 200:
                    raise RuntimeError(f"Ollama API error ({res.status_code}): {res.text}")

                raw_text = res.json().get("message", {}).get("content", "")
                raw_text_clean = raw_text.strip()
                if "```" in raw_text_clean:
                    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text_clean)
                    if match:
                        raw_text_clean = match.group(1).strip()

                parsed_json = json.loads(raw_text_clean)
                if isinstance(parsed_json, list):
                    parsed_json = {"test_cases": parsed_json}
                elif isinstance(parsed_json, dict):
                    if "test_cases" not in parsed_json:
                        for alt_key in ["cases", "testCases", "test_case_list", "tests", "data"]:
                            if alt_key in parsed_json and isinstance(parsed_json[alt_key], list):
                                parsed_json["test_cases"] = parsed_json.pop(alt_key)
                                break

                return response_model.model_validate(parsed_json)
            except Exception as e:
                last_exception = e
                logger.warning(f"[OLLAMA_CLIENT] Attempt {attempt} error: {repr(e)}. Retrying in 2.0s...")
                await asyncio.sleep(2.0)

        raise last_exception or RuntimeError("Ollama failed to generate structured output.")

    async def embed(self, text: str, input_type: str = "search_query") -> List[float]:
        """Generate text embedding using local Ollama embeddings endpoint."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": self.model, "prompt": text}
                )
                if res.status_code == 200:
                    return res.json().get("embedding", [])
        except Exception as e:
            logger.warning(f"[OLLAMA_CLIENT] Embeddings failed ({e}), using zero vector.")
        return [0.0] * 384
