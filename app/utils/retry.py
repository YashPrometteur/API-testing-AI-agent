import asyncio
import logging
from typing import Callable, TypeVar

T = TypeVar("T")
logger = logging.getLogger("retry")


async def retry_with_backoff(
    func: Callable[[], T],
    max_retries: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0
) -> T:
    """Deterministic retry helper with exponential backoff."""
    delay = initial_delay
    for attempt in range(1, max_retries + 1):
        try:
            if asyncio.iscoroutinefunction(func):
                return await func()
            return func()
        except Exception as e:
            if attempt == max_retries:
                raise e
            logger.warning(f"Attempt {attempt} failed with {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)
            delay *= backoff_factor
