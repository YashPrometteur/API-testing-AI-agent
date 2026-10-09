import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger("llm_rate_limiter")


class LLMRateLimiter:
    """
    Global FIFO Rate Limiter and Request Pacer for LLM APIs (Groq / OpenRouter).
    Guarantees strict concurrency limits, inter-request pacing, and automatic 
    backoff pausing across multiple concurrent runs.
    """

    MAX_COOLDOWN_SECONDS: float = 45.0

    def __init__(self, max_concurrency: int = 1, min_interval_seconds: float = 2.0):
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.min_interval_seconds = min_interval_seconds
        self._last_request_time: float = 0.0
        self._rate_limit_reset_until: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Acquire lock, wait for any active rate-limit backoff (capped at 45s max), and enforce minimum pacing."""
        await self.semaphore.acquire()
        async with self._lock:
            # 1. Respect active rate-limit block (capped at MAX_COOLDOWN_SECONDS)
            now = time.time()
            if now < self._rate_limit_reset_until:
                wait_time = min(self._rate_limit_reset_until - now, self.MAX_COOLDOWN_SECONDS)
                if wait_time > 0.1:
                    logger.info(f"[RATE_LIMITER] Respecting rate limit cooldown: waiting {wait_time:.2f}s...")
                    await asyncio.sleep(wait_time)

            # 2. Enforce minimum pacing interval between requests
            elapsed = time.time() - self._last_request_time
            if elapsed < self.min_interval_seconds:
                pacing_delay = self.min_interval_seconds - elapsed
                await asyncio.sleep(pacing_delay)

            self._last_request_time = time.time()

    def release(self):
        """Release the concurrency semaphore."""
        self.semaphore.release()

    def notify_rate_limit(self, cooldown_seconds: float):
        """
        Notify the rate limiter of a rate limit event.
        Only pause queued requests if cooldown is short (<= 45s, e.g. TPM burst pacing).
        For long cooldowns (> 45s, e.g. daily TPD exhaustion), do NOT sleep queued requests
        so they fail fast to deterministic fallback immediately.
        """
        if cooldown_seconds <= self.MAX_COOLDOWN_SECONDS:
            now = time.time()
            self._rate_limit_reset_until = max(self._rate_limit_reset_until, now + cooldown_seconds)
            logger.warning(f"[RATE_LIMITER] Short-term TPM rate limit: pausing queued requests for {cooldown_seconds:.2f}s.")
        else:
            logger.warning(
                f"[RATE_LIMITER] Long-term quota exhaustion ({cooldown_seconds:.1f}s > {self.MAX_COOLDOWN_SECONDS}s). "
                f"Skipping wait lock so subsequent requests fail fast to fallback."
            )


# Global singleton instance for project-wide LLM rate limiting
global_rate_limiter = LLMRateLimiter(max_concurrency=1, min_interval_seconds=2.0)
