import logging
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.tools.spec_parser import SpecParseError
from app.llm.structured_output import StructuredOutputFailure

logger = logging.getLogger("error_handler")


class RunNotFoundError(Exception):
    """Exception raised when a requested test run is not found in the database."""
    pass


def setup_exception_handlers(app: FastAPI):
    """Register custom FastAPI exception handlers to map exceptions to appropriate HTTP responses."""

    @app.exception_handler(SpecParseError)
    async def spec_parse_error_handler(request: Request, exc: SpecParseError):
        correlation_id = getattr(request.state, "correlation_id", "unknown")
        logger.warning(
            f"SpecParseError request={request.method} {request.url.path} correlation_id={correlation_id}: {exc}"
        )
        return JSONResponse(
            status_code=400,
            content={
                "error": "SpecParseError",
                "message": str(exc),
                "correlation_id": correlation_id
            }
        )

    @app.exception_handler(RunNotFoundError)
    async def run_not_found_error_handler(request: Request, exc: RunNotFoundError):
        correlation_id = getattr(request.state, "correlation_id", "unknown")
        logger.warning(
            f"RunNotFoundError request={request.method} {request.url.path} correlation_id={correlation_id}: {exc}"
        )
        return JSONResponse(
            status_code=404,
            content={
                "error": "RunNotFoundError",
                "message": str(exc),
                "correlation_id": correlation_id
            }
        )

    @app.exception_handler(StructuredOutputFailure)
    async def structured_output_failure_handler(request: Request, exc: StructuredOutputFailure):
        correlation_id = getattr(request.state, "correlation_id", "unknown")
        logger.warning(
            f"StructuredOutputFailure request={request.method} {request.url.path} correlation_id={correlation_id}: {exc}"
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": "StructuredOutputFailure",
                "message": str(exc),
                "correlation_id": correlation_id
            }
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        correlation_id = getattr(request.state, "correlation_id", "unknown")
        logger.error(
            f"Unhandled exception request={request.method} {request.url.path} correlation_id={correlation_id}: {exc}",
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal Server Error",
                "correlation_id": correlation_id
            }
        )
