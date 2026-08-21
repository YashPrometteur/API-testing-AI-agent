import asyncio
import functools
import json
import logging
import time
from typing import Any, Dict
from datetime import datetime, timezone
import uuid
from contextvars import ContextVar
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("logging_middleware")

ctx_run_id: ContextVar[str] = ContextVar("run_id", default="")
ctx_node: ContextVar[str] = ContextVar("node", default="")
ctx_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


class JSONFormatter(logging.Formatter):
    """Custom logging Formatter that outputs every log line as a single structured JSON string."""
    def format(self, record: logging.LogRecord) -> str:
        run_id = ctx_run_id.get() or getattr(record, "run_id", "")
        node = ctx_node.get() or getattr(record, "node", "")
        correlation_id = ctx_correlation_id.get() or getattr(record, "correlation_id", "")

        log_payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "run_id": run_id,
            "node": node,
            "correlation_id": correlation_id,
            "message": record.getMessage(),
        }

        if record.exc_info:
            log_payload["exception"] = self.formatException(record.exc_info)
        
        # Capture custom dictionary keys as context
        context = {}
        for k, v in record.__dict__.items():
            if k not in (
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module", "msecs",
                "msg", "name", "pathname", "process", "processName", "relativeCreated",
                "stack_info", "thread", "threadName", "run_id", "node", "correlation_id"
            ):
                context[k] = v
        if context:
            log_payload["context"] = context
            
        return json.dumps(log_payload)


def setup_logging():
    """Setup root logging to output in JSON format."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    
    # Force uvicorn loggers to propagate through root
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        l = logging.getLogger(name)
        l.handlers = []
        l.propagate = True


class LoggingMiddleware(BaseHTTPMiddleware):
    """FastAPI Middleware to set correlation ID and log request entry/exit."""
    async def dispatch(self, request: Request, call_next):
        corr_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        token_corr = ctx_correlation_id.set(corr_id)
        # Store correlation_id on request state so exception handlers can query it
        request.state.correlation_id = corr_id
        
        logger.info(f"Incoming request {request.method} {request.url.path}")
        start_time = time.perf_counter()
        try:
            response = await call_next(request)
            duration = (time.perf_counter() - start_time) * 1000.0
            logger.info(
                f"Completed request {request.method} {request.url.path} with status {response.status_code}",
                extra={"duration_ms": round(duration, 2)}
            )
            response.headers["X-Correlation-ID"] = corr_id
            return response
        except Exception as e:
            duration = (time.perf_counter() - start_time) * 1000.0
            logger.error(
                f"Request failed {request.method} {request.url.path} after {duration:.2f}ms: {e}",
                exc_info=True
            )
            raise
        finally:
            ctx_correlation_id.reset(token_corr)


def instrument_node(node_name: str):
    """Decorator to instrument graph nodes: sets context variables and logs entry/exit duration."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(state: Any, *args, **kwargs):
            run_id = ""
            if isinstance(state, dict):
                run_id = state.get("run_id", "")
            elif hasattr(state, "get"):
                run_id = state.get("run_id", "")
            else:
                run_id = getattr(state, "run_id", "")

            token_run = ctx_run_id.set(run_id)
            token_node = ctx_node.set(node_name)
            
            logger.info(f"Entering node '{node_name}'")
            start_time = time.perf_counter()
            try:
                import inspect
                if asyncio.iscoroutinefunction(func) or (hasattr(func, "__call__") and inspect.iscoroutinefunction(getattr(func, "__call__"))):
                    result = await func(state, *args, **kwargs)
                else:
                    result = func(state, *args, **kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                duration = (time.perf_counter() - start_time) * 1000.0
                logger.info(
                    f"Exiting node '{node_name}'",
                    extra={"duration_ms": round(duration, 2)}
                )
                return result
            except Exception as e:
                from langgraph.errors import GraphInterrupt
                if isinstance(e, GraphInterrupt):
                    # GraphInterrupt is normal LangGraph HITL control flow — not an error
                    raise
                duration = (time.perf_counter() - start_time) * 1000.0
                logger.error(
                    f"Error in node '{node_name}' after {duration:.2f}ms: {e}",
                    exc_info=True
                )
                raise
            finally:
                ctx_run_id.reset(token_run)
                ctx_node.reset(token_node)
        return wrapper
    return decorator
