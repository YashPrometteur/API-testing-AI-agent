import logging
from typing import List, Optional
from fastapi import APIRouter, status

from app.config.settings import settings
from app.repositories.defect_repository import DefectRepository

logger = logging.getLogger("defects_api")
router = APIRouter(prefix="/defects")


@router.get("")
async def list_defects(
    run_id: Optional[str] = None,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    spec_id: Optional[str] = None,
    page: int = 1,
    limit: int = 100
):
    """Get paginated list of defects filtered by run_id, status, severity, or spec_id."""
    from app.database import get_mongo_db
    db = await get_mongo_db()
    defect_repo = DefectRepository(db)

    skip = (page - 1) * limit
    defects = await defect_repo.find_defects(
        run_id=run_id,
        status=status,
        severity=severity,
        spec_id=spec_id,
        skip=skip,
        limit=limit
    )

    # Format database records to API response schema
    items = []
    for doc in defects:
        items.append({
            "defect_id": doc["_id"],
            "run_id": doc.get("run_id"),
            "test_case_id": doc.get("test_case_id"),
            "severity": doc.get("severity"),
            "confidence": doc.get("confidence"),
            "status": doc.get("status"),
            "title": doc.get("title"),
            "root_cause_summary": doc.get("root_cause_summary"),
            "created_at": doc.get("created_at")
        })

    return {
        "page": page,
        "limit": limit,
        "items": items
    }
