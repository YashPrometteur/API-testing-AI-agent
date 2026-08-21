from fastapi import FastAPI, HTTPException

from app.config.settings import settings
from app.api.v1 import specs, runs, defects, reports, reviews, websocket
from app.middleware.logging_middleware import LoggingMiddleware, setup_logging
from app.middleware.error_handler import setup_exception_handlers

# Configure structured JSON logging on startup
setup_logging()

app = FastAPI(title="AI API Testing & Validation Agent", version="0.2.0")

# Register global exception handlers and logging middleware
setup_exception_handlers(app)
app.add_middleware(LoggingMiddleware)

# Include API v1 routers
app.include_router(specs.router, prefix="/api/v1")
app.include_router(runs.router, prefix="/api/v1")
app.include_router(defects.router, prefix="/api/v1")
app.include_router(reports.router, prefix="/api/v1")
app.include_router(reviews.router, prefix="/api/v1")
app.include_router(websocket.router, prefix="/api/v1")


@app.get("/health")
async def health_check():
    from app.database import check_db_health
    status_str, mongo_msg, redis_msg = await check_db_health()
    return {
        "status": status_str,
        "mongo": mongo_msg,
        "redis": redis_msg
    }


@app.on_event("startup")
async def cleanup_stale_runs_on_startup():
    """Cleanup any orphaned runs left in 'running' status due to server restarts."""
    try:
        from app.database import get_mongo_db
        from app.utils.datetime import current_utc_iso
        db = await get_mongo_db()
        result = await db["runs"].update_many(
            {"status": "running"},
            {
                "$set": {
                    "status": "failed",
                    "errors": ["Server process was restarted or terminated during run execution."],
                    "updated_at": current_utc_iso()
                }
            }
        )
        if result.modified_count > 0:
            import logging
            logging.getLogger("main").info(f"[STARTUP] Cleaned up {result.modified_count} stale running runs.")
    except Exception as e:
        import logging
        logging.getLogger("main").warning(f"[STARTUP] Failed to clean up stale runs on startup: {e}")


