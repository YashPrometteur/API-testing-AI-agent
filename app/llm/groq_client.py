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
from app.llm.rate_limiter import global_rate_limiter

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("groq_client")


AVAILABLE_GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b"
]


class GroqClient:
    """Groq client implementation using OpenAI-compatible structured outputs / JSON mode with 3-model 600k token pool resilience."""

    # Class-level current model index so failovers persist across generator calls
    _shared_model_idx: int = 0
    _exhausted_models_until: dict = {}

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or settings.GROQ_API_KEY
        configured_model = model or settings.GROQ_MODEL
        # Build priority pool: configured model first, followed by remaining models
        pool = [configured_model] if configured_model else []
        for m in AVAILABLE_GROQ_MODELS:
            if m not in pool:
                pool.append(m)
        self.models_pool = pool
        self.model = self.models_pool[GroqClient._shared_model_idx % len(self.models_pool)]
        self.client = AsyncGroq(api_key=self.api_key, max_retries=0) if self.api_key else None

    async def generate_structured(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str = "",
        temperature: float = 0.2
    ) -> T:
        import time
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

        last_exception = None
        pool_len = len(self.models_pool)

        for model_turn in range(pool_len):
            current_model = self.models_pool[GroqClient._shared_model_idx % pool_len]
            self.model = current_model

            # Skip models that are known to be in daily quota cooldown
            now = time.time()
            if now < GroqClient._exhausted_models_until.get(current_model, 0):
                GroqClient._shared_model_idx = (GroqClient._shared_model_idx + 1) % pool_len
                continue

            model_exhausted = False
            max_attempts = 5

            for attempt in range(1, max_attempts + 1):
                try:
                    # Pacing & Concurrency Queuing via global rate limiter
                    await global_rate_limiter.acquire()
                    try:
                        response = await self.client.chat.completions.create(
                            model=self.model,
                            messages=messages,
                            response_format={"type": "json_object"},
                            temperature=temperature,
                        )
                    finally:
                        global_rate_limiter.release()
                    raw_text = response.choices[0].message.content or ""
                    logger.info(f"[GROQ_CLIENT] Raw response received ({len(raw_text)} chars) from {self.model}: {raw_text[:250]}...")
                    
                    # Robust extraction of JSON from raw response
                    raw_text_clean = raw_text.strip()
                    if "```" in raw_text_clean:
                        import re
                        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text_clean)
                        if match:
                            raw_text_clean = match.group(1).strip()
                    
                    # Parse with json first to normalize any wrapper irregularities
                    try:
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
                    except Exception as parse_err:
                        logger.error(f"[GROQ_CLIENT] JSON/Schema Validation Error: {parse_err}\n[RAW LLM RESPONSE WAS]:\n{raw_text}")
                        raise parse_err
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
                    err_str = str(e)

                    # 1. First priority: Parse exact retry delay from Groq's error message (e.g. 'Please try again in 14.2s')
                    delay = None
                    import re
                    m_sec = re.search(r'try again in (?:(\d+)m\s*)?([\d\.]+)s', err_str, re.IGNORECASE)
                    if m_sec:
                        mins = float(m_sec.group(1) or 0)
                        secs = float(m_sec.group(2) or 0)
                        delay = (mins * 60.0) + secs + 0.5
                    else:
                        m_ms = re.search(r'try again in ([\d\.]+)ms', err_str, re.IGNORECASE)
                        if m_ms:
                            delay = (float(m_ms.group(1)) / 1000.0) + 0.2

                    # 2. Second priority: retry-after or reset headers from Groq API
                    if delay is None:
                        retry_after_header = headers.get("retry-after")
                        if retry_after_header:
                            try:
                                delay = float(retry_after_header) + random.uniform(0.2, 0.6)
                            except (ValueError, TypeError):
                                pass

                    if delay is None:
                        reset_tokens = headers.get("x-ratelimit-reset-tokens")
                        if reset_tokens:
                            try:
                                if reset_tokens.endswith("ms"):
                                    delay = (float(reset_tokens[:-2]) / 1000.0) + random.uniform(0.2, 0.5)
                                elif reset_tokens.endswith("s"):
                                    delay = float(reset_tokens[:-1]) + random.uniform(0.2, 0.6)
                            except (ValueError, TypeError):
                                pass

                    # 3. Fallback: exponential backoff
                    if delay is None:
                        base_delay = 2.0 ** attempt
                        delay = base_delay + random.uniform(0.5, 1.5)

                    # Notify global rate limiter to pause other waiting coroutines
                    global_rate_limiter.notify_rate_limit(delay)

                    is_daily = delay > 30.0 or "tokens per day" in err_str.lower() or "tpd" in err_str.lower()
                    if is_daily:
                        logger.warning(
                            f"[GROQ_CLIENT] Model '{current_model}' daily quota reached ({delay:.1f}s delay requested). "
                            f"Seamlessly failing over to next model in 600k token pool..."
                        )
                        GroqClient._exhausted_models_until[current_model] = now + max(delay, 600.0)
                        model_exhausted = True
                        break

                    if attempt < 2 and delay <= 3.0:
                        logger.warning(
                            f"[GROQ_CLIENT] Rate Limit / Transport pause on model '{self.model}' (attempt={attempt}/2, "
                            f"status={status_code}). Sleeping {delay:.2f}s for sliding token window to refresh..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.warning(
                            f"[GROQ_CLIENT] Model '{current_model}' paused ({delay:.1f}s delay requested). "
                            f"Rotating immediately to next provider/model in pool..."
                        )
                        GroqClient._exhausted_models_until[current_model] = now + max(delay, 30.0)
                        model_exhausted = True
                        break
                except ValidationError as e:
                    # Let ValidationError bubble up for structured_output re-prompt handling
                    raise e
                except Exception as e:
                    raise e

            if model_exhausted:
                GroqClient._shared_model_idx = (GroqClient._shared_model_idx + 1) % pool_len
                next_model = self.models_pool[GroqClient._shared_model_idx]
                logger.warning(
                    f"[GROQ_CLIENT] Seamlessly switched active model to '{next_model}' "
                    f"({GroqClient._shared_model_idx + 1}/{pool_len} in 600k token pool). Continuing mid-run..."
                )
                continue

        # Tier 2 Safety Net: Seamless failover to OpenAI (gpt-4o-mini)
        now = time.time()
        if settings.OPENAI_API_KEY and now >= GroqClient._exhausted_models_until.get("openai", 0):
            try:
                logger.warning("[GROQ_CLIENT -> FAILOVER] Groq models exhausted. Failing over to OpenAI (gpt-4o-mini)...")
                from app.llm.openai_client import OpenAIClient
                openai_client = OpenAIClient(model="gpt-4o-mini")
                return await openai_client.generate_structured(
                    prompt=prompt,
                    response_model=response_model,
                    system_prompt=system_prompt,
                    temperature=temperature
                )
            except Exception as openai_err:
                logger.warning(f"[GROQ_CLIENT -> FAILOVER] OpenAI fallback unavailable ({openai_err}). Trying OpenRouter...")
                GroqClient._exhausted_models_until["openai"] = now + 600.0

        # Tier 3 Safety Net: Seamless failover to OpenRouter Cloud
        if settings.OPENROUTER_API_KEY and now >= GroqClient._exhausted_models_until.get("openrouter", 0):
            try:
                logger.warning("[GROQ_CLIENT -> FAILOVER] Failing over to OpenRouter Cloud...")
                from app.llm.openrouter_client import OpenRouterClient
                openrouter_client = OpenRouterClient()
                return await openrouter_client.generate_structured(
                    prompt=prompt,
                    response_model=response_model,
                    system_prompt=system_prompt,
                    temperature=temperature
                )
            except Exception as openrouter_err:
                logger.warning(f"[GROQ_CLIENT -> FAILOVER] OpenRouter fallback unavailable ({openrouter_err}). Trying local Ollama...")
                GroqClient._exhausted_models_until["openrouter"] = now + 600.0

        # Tier 4 Hybrid Safety Net: Seamless failover to local Ollama (qwen2.5:7b)
        if now >= GroqClient._exhausted_models_until.get("ollama", 0):
            try:
                logger.warning("[GROQ_CLIENT -> HYBRID] Failing over to local Ollama (qwen2.5:7b)...")
                from app.llm.ollama_client import OllamaClient
                ollama = OllamaClient()
                return await ollama.generate_structured(
                    prompt=prompt,
                    response_model=response_model,
                    system_prompt=system_prompt,
                    temperature=temperature
                )
            except Exception as ollama_err:
                logger.error(f"[GROQ_CLIENT -> HYBRID] Local Ollama fallback failed: {ollama_err}")
                GroqClient._exhausted_models_until["ollama"] = now + 600.0
                raise last_exception or ollama_err

        raise last_exception or RuntimeError("All LLM providers exhausted for current request window.")

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

