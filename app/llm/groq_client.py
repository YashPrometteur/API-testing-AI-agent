import asyncio
import json
import logging
import random
from typing import List, Type, TypeVar
from pydantic import BaseModel, ValidationError
try:
    from groq import AsyncGroq, APIError, RateLimitError, APIConnectionError
except ImportError:
    AsyncGroq = None
    APIError = Exception
    RateLimitError = Exception
    APIConnectionError = Exception

from app.config.settings import settings

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("groq_client")


# Shared module-level concurrency semaphore to limit concurrent active Groq requests across all agent tasks
_groq_semaphore = asyncio.Semaphore(3)


class GroqClient:
    """Groq client implementation using OpenAI-compatible structured outputs / JSON mode with rate-limit resilience."""

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or settings.GROQ_API_KEY
        self.model = model or settings.GROQ_MODEL
        self.client = AsyncGroq(api_key=self.api_key) if self.api_key else None

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str = "",
        temperature: float = 0.2
    ) -> T:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not configured in settings or environment.")

        # Append JSON schema instruction to system prompt for Groq JSON mode
        json_schema = json.dumps(response_model.model_json_schema(), indent=2)
        schema_instruction = (
            f"\n\nCRITICAL: You MUST respond ONLY with a valid JSON object strictly matching this JSON schema:\n"
            f"```json\n{json_schema}\n```\nDo not include any commentary, markdown wrapping, or raw text outside the JSON object."
        )
        full_system_prompt = (system_prompt or "You are a precise AI API Testing Agent.") + schema_instruction

        messages = [
            {"role": "system", "content": full_system_prompt},
            {"role": "user", "content": prompt}
        ]

        max_attempts = 4
        last_exception = None

        for attempt in range(1, max_attempts + 1):
            try:
                # Throttling concurrent active API requests using shared semaphore
                async with _groq_semaphore:
                    response = await self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        response_format={"type": "json_object"},
                        temperature=temperature,
                    )
                raw_text = response.choices[0].message.content or ""
                # Strip triple backticks if present
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
                status_code = getattr(e, "status_code", None)
                headers = {}
                if hasattr(e, "response") and e.response is not None:
                    if not status_code:
                        status_code = getattr(e.response, "status_code", None)
                    headers = getattr(e.response, "headers", {}) or {}

                status_code = status_code or (429 if isinstance(e, RateLimitError) else 500)
                req_id = headers.get("x-request-id", "unknown_req_id")

                if attempt < max_attempts:
                    # Check for Retry-After or reset headers from Groq API
                    retry_after_header = headers.get("retry-after") or headers.get("x-ratelimit-reset-requests")
                    delay = None
                    if retry_after_header:
                        try:
                            # Try parsing numerical seconds
                            delay = float(retry_after_header) + random.uniform(0.1, 0.5)
                        except (ValueError, TypeError):
                            pass

                    if delay is None:
                        # Exponential backoff with randomized jitter: 2^attempt + jitter
                        base_delay = 2.0 ** attempt
                        delay = base_delay + random.uniform(0.2, 1.2)

                    if delay > 10.0:
                        logger.warning(
                            f"[GROQ_CLIENT] Excessive rate limit delay requested ({delay:.1f}s > 10s max). "
                            f"Failing request immediately to trigger fast baseline generator fallback."
                        )
                        raise e

                    logger.warning(
                        f"[GROQ_CLIENT] Transport / Rate Limit Error (status={status_code}, attempt={attempt}/{max_attempts}, "
                        f"req_id='{req_id}'). Model='{self.model}'. Error='{e}'. Retrying in {delay:.2f}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        f"[GROQ_CLIENT] Exhausted all {max_attempts} retries for Groq API (status={status_code}, req_id='{req_id}'). "
                        f"Error: {e}"
                    )
                    raise e
            except ValidationError as e:
                # Let ValidationError bubble up for structured_output re-prompt handling
                raise e
            except Exception as e:
                raise e

        raise last_exception or RuntimeError("Failed to generate structured output from Groq.")

    async def embed(self, text: str, input_type: str = "search_query") -> List[float]:
        """Generate text embedding using Cohere API if configured, otherwise local sentence-transformers with fallback."""
        global _local_embedding_model, _use_fallback_embedder

        from app.config.settings import settings
        if settings.COHERE_API_KEY:
            try:
                import httpx
                headers = {
                    "Authorization": f"Bearer {settings.COHERE_API_KEY}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "texts": [text],
                    "model": "embed-english-v3.0",
                    "input_type": input_type
                }
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.post("https://api.cohere.ai/v1/embed", json=payload, headers=headers)
                    if res.status_code == 200:
                        data = res.json()
                        embedding = data["embeddings"][0]
                        return [float(val) for val in embedding]
                    else:
                        logger.warning(f"[GROQ_CLIENT] Cohere API error ({res.status_code}). Falling back to local/Feature Hashing.")
            except Exception as e:
                logger.warning(f"[GROQ_CLIENT] Cohere API call failed ({e}). Falling back to local/Feature Hashing.")

        if _use_fallback_embedder:
            dim = 1024 if settings.COHERE_API_KEY else 384
            return self._fallback_embed(text, dimension=dim)

        try:
            if _local_embedding_model is None:
                from sentence_transformers import SentenceTransformer
                _local_embedding_model = SentenceTransformer("all-MiniLM-L6-v2")

            embedding = await asyncio.to_thread(_local_embedding_model.encode, text)
            return [float(val) for val in embedding]
        except (ImportError, OSError, Exception) as e:
            logger.warning(
                f"[GROQ_CLIENT] Local sentence-transformers failed to load ({e}). "
                f"Falling back to pure-Python Feature Hashing Embedder."
            )
            _use_fallback_embedder = True
            dim = 1024 if settings.COHERE_API_KEY else 384
            return self._fallback_embed(text, dimension=dim)

    def _fallback_embed(self, text: str, dimension: int = 384) -> List[float]:
        """Pure-Python Feature Hashing (Hashing Trick) embedder fallback."""
        import hashlib
        if not text:
            return [0.0] * dimension

        words = [w.lower().strip(".,!?\"'") for w in text.split()]
        words = [w for w in words if len(w) > 2]

        vector = [0.0] * dimension
        if not words:
            words = [text[i:i+3] for i in range(len(text) - 2)]
            if not words:
                words = [text]

        for w in words:
            idx = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16) % dimension
            vector[idx] += 1.0

        # Normalize L2
        norm = sum(x*x for x in vector) ** 0.5
        if norm > 0:
            vector = [x / norm for x in vector]
        return vector


# Global lazy-loaded local embedding model instance
_local_embedding_model = None


def check_sentence_transformers_support() -> bool:
    """Run a quick, isolated import check in a subprocess to avoid DLL load crashes in the main process."""
    import subprocess
    import sys
    try:
        res = subprocess.run(
            [sys.executable, "-c", "import sentence_transformers"],
            capture_output=True,
            text=True,
            timeout=2.0
        )
        return res.returncode == 0
    except Exception:
        return False


_use_fallback_embedder = not check_sentence_transformers_support()
if _use_fallback_embedder:
    from app.config.settings import settings
    if not settings.COHERE_API_KEY:
        logger.warning("[GROQ_CLIENT] Isolated check failed for sentence-transformers. Using pure-Python Feature Hashing Embedder.")

