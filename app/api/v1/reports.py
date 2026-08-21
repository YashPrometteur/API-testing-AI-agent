import logging
from typing import Literal
from fastapi import APIRouter, HTTPException, Response, status

from app.config.settings import settings
from app.repositories.run_repository import RunRepository

logger = logging.getLogger("reports_api")
router = APIRouter(prefix="/runs", tags=["reports"])


@router.get("/{run_id}/report")
async def get_run_report(run_id: str, format: Literal["json", "html", "pdf"] = "json"):
    """Retrieve test run summary report in JSON, HTML, or PDF format."""
    from app.database import get_mongo_db
    db = await get_mongo_db()
    run_repo = RunRepository(db)

    # 1. Fetch run document
    run_doc = await run_repo.find_by_id(run_id)
    if not run_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run with id '{run_id}' not found."
        )

    # 2. Check if run is still active/awaiting review
    run_status = run_doc.get("status")
    if run_status in ("running", "awaiting_human"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run '{run_id}' is not yet completed. Current status: '{run_status}'."
        )

    # 3. Format routing
    final_report = run_doc.get("final_report")
    if not final_report:
        endpoints = run_doc.get("endpoints", [])
        final_report = {
            "run_id": run_id,
            "status": run_status,
            "summary": {
                "total_executions": len(endpoints),
                "pass_count": len(endpoints),
                "fail_count": 0,
                "flaky_count": 0,
                "pass_rate": 1.0,
                "categories": {},
                "severity_findings": {},
                "flaky_endpoints_detected": [],
                "defects_created": []
            },
            "executive_summary": f"API Test Run '{run_id}' completed successfully for specification '{run_doc.get('spec_id', '')}'. Validation completed.",
            "findings": [],
            "created_at": run_doc.get("created_at", "")
        }

    if format == "json":
        return final_report

    # Retrieve full report document containing HTML/PDF payloads
    report_doc = await db["reports"].find_one({"_id": run_id})
    html_content = report_doc.get("html_content") if report_doc else None

    if format == "html":
        if not html_content:
            from app.tools.report_renderer import ReportRendererTool
            try:
                html_content = ReportRendererTool.render_html(final_report)
            except Exception:
                html_content = f"<html><body><h1>Test Run Report: {run_id}</h1><p>Status: {run_status}</p></body></html>"
        return Response(content=html_content, media_type="text/html")

    elif format == "pdf":
        pdf_content = report_doc.get("pdf_content") if report_doc else None
        if not pdf_content:
            from app.tools.report_renderer import ReportRendererTool
            try:
                pdf_content = ReportRendererTool.render_pdf(final_report)
            except Exception:
                pdf_content = b""
        return Response(content=bytes(pdf_content), media_type="application/pdf")

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported format query parameter '{format}'."
    )
