import logging
import time
from typing import Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("circuit_breaker")


class HostCircuitBreaker:
    """Tracks consecutive transient failures per target host and trips after max_failures.

    Supports automatic recovery via standard half-open cooldown probing.
    """

    def __init__(self, max_failures: int = 5, cooldown_seconds: float = 30.0):
        self.max_failures = max_failures
        self.cooldown_seconds = cooldown_seconds
        self._failure_counts: Dict[str, int] = {}
        self._tripped_hosts: Dict[str, bool] = {}
        self._last_failure_time: Dict[str, float] = {}

    def extract_host(self, url: str) -> str:
        """Extract host/netloc from URL."""
        parsed = urlparse(url)
        return parsed.netloc or parsed.path.split("/")[0]

    def is_open(self, url_or_host: str) -> bool:
        """Check if circuit breaker is open for the given URL or host.

        If cooldown has elapsed since the last failure, enters half-open state and returns
        False to allow a single probe request through.
        """
        host = self.extract_host(url_or_host) if "://" in url_or_host else url_or_host
        if not self._tripped_hosts.get(host, False):
            return False

        last_time = self._last_failure_time.get(host, 0.0)
        elapsed = time.monotonic() - last_time

        if elapsed >= self.cooldown_seconds:
            logger.info(
                f"[CIRCUIT_BREAKER] Host '{host}' entering half-open probe after "
                f"{elapsed:.1f}s cooldown (threshold: {self.cooldown_seconds}s)."
            )
            return False

        return True

    def record_success(self, url_or_host: str) -> None:
        """Record a successful (or non-transient) request, resetting consecutive failure count and closing the circuit."""
        host = self.extract_host(url_or_host) if "://" in url_or_host else url_or_host
        was_tripped = self._tripped_hosts.get(host, False)
        
        self._failure_counts[host] = 0
        self._tripped_hosts[host] = False
        self._last_failure_time.pop(host, None)

        if was_tripped:
            logger.info(f"[CIRCUIT_BREAKER] Half-open probe succeeded for host '{host}'. Circuit breaker fully recovered and closed.")

    def record_transient_failure(self, url_or_host: str) -> bool:
        """Record a transient failure. Returns True if circuit tripped."""
        host = self.extract_host(url_or_host) if "://" in url_or_host else url_or_host
        current = self._failure_counts.get(host, 0) + 1
        self._failure_counts[host] = current
        self._last_failure_time[host] = time.monotonic()

        if current >= self.max_failures:
            was_already_tripped = self._tripped_hosts.get(host, False)
            self._tripped_hosts[host] = True
            if was_already_tripped:
                logger.warning(
                    f"[CIRCUIT_BREAKER] Half-open probe failed for host '{host}'. "
                    f"Re-tripping circuit breaker for another {self.cooldown_seconds}s cooldown."
                )
            else:
                logger.warning(
                    f"[CIRCUIT_BREAKER] Tripped circuit breaker for host '{host}' "
                    f"after {current} consecutive transient failures!"
                )
            return True
        return False

    def reset(self) -> None:
        """Reset all circuit breaker states."""
        self._failure_counts.clear()
        self._tripped_hosts.clear()
        self._last_failure_time.clear()

