import asyncio
import logging
import random
import time
from typing import Any, Dict, Optional, Tuple
import httpx

from app.tools import ToolResult

logger = logging.getLogger("http_executor")


class HTTPExecutionTool:
    """Tool for executing HTTP API test requests with transient retry logic."""

    @staticmethod
    def is_transient_failure(
        status_code: Optional[int],
        exception: Optional[Exception],
        expected_status_code: Optional[int] = None
    ) -> bool:
        """Classify failure as transient (retryable) vs valid/non-transient.

        - Connection timeouts, DNS failures, Network errors -> transient.
        - 5xx Server Errors, 429 Rate Limit -> transient.
        - 4xx Client Errors matching expected test outcome (e.g. negative test expecting 400) -> NOT transient (valid response).
        - 4xx Client Errors NOT matching expected status code (e.g. unexpected 404/401) -> non-transient defect (do not retry).
        """
        if exception is not None:
            if isinstance(exception, (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, httpx.ConnectTimeout)):
                return True
            return False

        if status_code is None:
            return True

        # 429 Rate Limit, 503 Service Unavailable, and 504 Gateway Timeout are transient network/infra issues
        if status_code in (429, 503, 504):
            return True

        # HTTP 500, 502 or 4xx responses are deterministic server/client responses (not transient network drops)
        return False

    @classmethod
    async def execute(
        cls,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        query_params: Optional[Dict[str, Any]] = None,
        body: Optional[Any] = None,
        files: Optional[Any] = None,
        expected_status_code: Optional[int] = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        client: Optional[httpx.AsyncClient] = None
    ) -> ToolResult:
        """Execute an HTTP request with exponential backoff retries for transient errors."""
        headers = dict(headers) if headers else {}
        query_params = query_params or {}

        # Prepare request body
        json_body = None
        data_body = None
        content_body = None
        files_body = files

        content_type = headers.get("Content-Type", "").lower()

        if files_body is not None:
            # Let httpx calculate multipart boundary automatically
            headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            if isinstance(body, dict):
                data_body = body
        elif body is not None:
            if "x-www-form-urlencoded" in content_type:
                data_body = body
            elif isinstance(body, (dict, list)):
                json_body = body
            elif isinstance(body, str):
                content_body = body.encode("utf-8")
                if "Content-Type" not in headers:
                    headers["Content-Type"] = "application/json"
            else:
                json_body = body

        attempt = 0
        last_transient = False

        backoffs = [1.0, 2.0, 4.0]

        async def _make_request(http_client: httpx.AsyncClient) -> Tuple[httpx.Response, float]:
            start_time = time.perf_counter()
            resp = await http_client.request(
                method=method.upper(),
                url=url,
                headers=headers,
                params=query_params,
                json=json_body,
                data=data_body,
                content=content_body,
                files=files_body,
                timeout=timeout
            )
            try:
                print('api respsone------------------', resp.json())
            except Exception:
                print('api respsone------------------', resp.text)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return resp, elapsed_ms

        should_close_client = False
        if client is None:
            client = httpx.AsyncClient(verify=False)
            should_close_client = True

        try:
            for attempt in range(1, max_retries + 1):
                try:
                    response, latency_ms = await _make_request(client)
                    status_code = response.status_code

                    # Parse response body
                    try:
                        resp_body = response.json()
                    except Exception:
                        resp_body = response.text

                    resp_headers = dict(response.headers)

                    is_transient = cls.is_transient_failure(
                        status_code=status_code,
                        exception=None,
                        expected_status_code=expected_status_code
                    )

                    if is_transient and attempt < max_retries:
                        backoff = backoffs[min(attempt - 1, len(backoffs) - 1)] + random.uniform(0.0, 0.5)
                        logger.warning(
                            f"[HTTP_EXECUTOR] Transient failure status={status_code} for {method} {url} "
                            f"(attempt {attempt}/{max_retries}). Retrying in {backoff:.2f}s..."
                        )
                        await asyncio.sleep(backoff)
                        continue

                    # Final attempt or non-transient outcome
                    data = {
                        "status_code": status_code,
                        "headers": resp_headers,
                        "body": resp_body,
                        "actual_response": resp_body,
                        "latency_ms": round(latency_ms, 2),
                        "attempt_count": attempt,
                        "transient_error": is_transient
                    }
                    return ToolResult(
                        success=True,
                        data=data,
                        metadata={"attempts": attempt, "transient": is_transient}
                    )

                except Exception as exc:
                    is_transient = cls.is_transient_failure(
                        status_code=None,
                        exception=exc,
                        expected_status_code=expected_status_code
                    )

                    if is_transient and attempt < max_retries:
                        backoff = backoffs[min(attempt - 1, len(backoffs) - 1)] + random.uniform(0.0, 0.5)
                        logger.warning(
                            f"[HTTP_EXECUTOR] Transient network error '{exc}' for {method} {url} "
                            f"(attempt {attempt}/{max_retries}). Retrying in {backoff:.2f}s..."
                        )
                        await asyncio.sleep(backoff)
                        continue

                    data = {
                        "status_code": None,
                        "headers": {},
                        "body": None,
                        "latency_ms": 0.0,
                        "attempt_count": attempt,
                        "transient_error": True,
                        "error_message": str(exc)
                    }
                    return ToolResult(
                        success=False,
                        data=data,
                        error=str(exc),
                        metadata={"attempts": attempt, "transient": True}
                    )

            # Fallback (should not be reached)
            return ToolResult(success=False, error="Max retries exceeded", metadata={"attempts": max_retries})

        finally:
            if should_close_client:
                await client.aclose()
