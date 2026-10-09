import asyncio
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, status, Request
from pydantic import BaseModel, Field

from app.config.settings import settings
from app.graph.build import build_graph
from app.repositories.run_repository import RunRepository
from app.repositories.spec_repository import SpecRepository
from app.repositories.finding_repository import FindingRepository
from app.repositories.execution_history_repository import ExecutionHistoryRepository
from app.graph.checkpointer import MongoCheckpointSaver
from app.utils.datetime import current_utc_iso
from app.utils.ids import generate_run_id
from app.database import get_mongo_db

logger = logging.getLogger("app.api.v1.runs")

router = APIRouter(prefix="/runs", tags=["runs"])


class CreateRunOptions(BaseModel):
    max_reflection_loops: Optional[int] = settings.MAX_REFLECTION_LOOPS
    min_confidence_threshold: Optional[float] = settings.MIN_CONFIDENCE_THRESHOLD


class CreateRunRequest(BaseModel):
    spec_id: str
    target_base_url: str
    categories: Optional[List[str]] = Field(default_factory=list)
    target_operations: Optional[List[str]] = Field(
        default_factory=list,
        description="List of operation IDs or endpoints to test (e.g. ['addPet', 'getPetById']). Leave empty to test all endpoints.",
        json_schema_extra={"example": ["addPet", "getPetById"]}
    )
    auth_config: Optional[Dict[str, Any]] = Field(default_factory=dict)
    options: Optional[CreateRunOptions] = Field(default_factory=CreateRunOptions)


async def _execute_run_background(
    run_id: str,
    spec_source: dict,
    spec_id: str = "",
    categories: list = None,
    auth_config: dict = None,
    target_operations: list = None,
    target_base_url: str = "",
    options: dict = None,
):
    try:
        from app.database import get_mongo_db, save_mock_db_to_disk
        db = await get_mongo_db()
        run_repo = RunRepository(db)
        checkpointer = MongoCheckpointSaver(db=db)
        graph = build_graph(checkpointer=checkpointer)

        initial_state = {
            "run_id": run_id,
            "spec_id": spec_id,
            "spec_source": spec_source,
            "categories": categories or [],
            "target_operations": target_operations or [],
            "target_base_url": target_base_url,
            "auth_config": auth_config or {},
            "options": options or {},
            "status": "running",
            "endpoints": [],
            "test_plan": {},
            "reflection_count": 0,
            "retry_counts": {},
            "errors": []
        }

        import logging
        lg = logging.getLogger("runs_api")
        lg.info(f"[RUNS_API] Initializing background execution for run '{run_id}'. categories={categories or []}, target_operations={target_operations or []}")

        config = {"configurable": {"thread_id": run_id}}
        graph_res = await graph.ainvoke(initial_state, config=config)

        final_status = "completed" if "final_report" in graph_res or graph_res.get("status") == "completed" else graph_res.get("status", "completed")
        update_data = {
            "endpoints": graph_res.get("endpoints", []),
            "test_plan": graph_res.get("test_plan", {}),
            "test_cases": graph_res.get("test_cases", []),
            "execution_results": graph_res.get("execution_results", []),
            "findings": graph_res.get("findings", []),
            "errors": graph_res.get("errors", []),
            "updated_at": current_utc_iso(),
        }
        if "test_case_executions" in graph_res:
            update_data["test_case_executions"] = graph_res["test_case_executions"]
        if "final_report" in graph_res:
            update_data["final_report"] = graph_res["final_report"]

        await run_repo.update_status(run_id, final_status, update_data)
        await save_mock_db_to_disk()
    except Exception as e:
        logger.error(f"[RUNS_API] Background graph execution failed for run '{run_id}': {e}")
        try:
            from app.database import get_mongo_db
            db = await get_mongo_db()
            run_repo = RunRepository(db)
            await run_repo.update_status(run_id, "failed", {"errors": [str(e)], "updated_at": current_utc_iso()})
        except Exception:
            pass
        await save_mock_db_to_disk()


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_run(payload: CreateRunRequest):
    db = await get_mongo_db()
    spec_repo = SpecRepository(db)
    run_repo = RunRepository(db)

    spec_doc = await spec_repo.get_active_spec(payload.spec_id)
    if not spec_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Specification with id '{payload.spec_id}' not found."
        )

    run_id = generate_run_id()
    created_at = current_utc_iso()

    # Auto-clean target_base_url if spec JSON URL was passed accidentally
    target_base_url = payload.target_base_url.strip()
    if target_base_url.endswith("/swagger.json"):
        target_base_url = target_base_url[:-13].rstrip("/")
    elif target_base_url.endswith("/openapi.json"):
        target_base_url = target_base_url[:-13].rstrip("/")

    run_doc = {
        "_id": run_id,
        "spec_id": payload.spec_id,
        "target_base_url": target_base_url,
        "status": "running",
        "created_at": created_at,
        "updated_at": created_at,
        "options": payload.options.model_dump() if payload.options else {},
        "categories": payload.categories or [],
        "target_operations": payload.target_operations or [],
        "auth_config": payload.auth_config or {},
    }

    await run_repo.insert(run_doc)
    from app.database import save_mock_db_to_disk
    await save_mock_db_to_disk()

    spec_source = spec_doc.get("raw_spec") or spec_doc.get("spec_data") or spec_doc.get("normalized_spec") or {}
    asyncio.create_task(_execute_run_background(
        run_id=run_id,
        spec_source=spec_source,
        spec_id=payload.spec_id,
        categories=payload.categories,
        auth_config=payload.auth_config,
        target_operations=payload.target_operations,
        target_base_url=target_base_url,
        options=payload.options.model_dump() if payload.options else {},
    ))

    return {
        "run_id": run_id,
        "status": "running",
        "created_at": created_at,
        "status_url": f"/api/v1/runs/{run_id}"
    }


@router.get("/{run_id}")
async def get_run(run_id: str):
    db = await get_mongo_db()
    run_repo = RunRepository(db)
    run_doc = await run_repo.find_by_id(run_id)
    if not run_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run with id '{run_id}' not found."
        )

    if run_doc.get("status") == "awaiting_human":
        finding_repo = FindingRepository(db)
        pending_findings = await finding_repo.find_pending_review_by_run(run_id)
        run_doc["pending_review_count"] = len(pending_findings)
        run_doc["pending_review_url"] = f"/api/v1/defects/pending?run_id={run_id}"

    return run_doc


@router.post("/{run_id}/approve")
@router.post("/{run_id}/approve-all")
@router.post("/{run_id}/bulk-approve")
async def approve_run(run_id: str, request: Request):
    """Approve all pending findings for a run in 1-click and resume execution to generate report."""
    from app.api.v1.reviews import bulk_approve_reviews
    return await bulk_approve_reviews(run_id=run_id, request=request)


@router.get("/{run_id}/history")
async def get_run_history(run_id: str):
    """Get endpoint flakiness and overall pass rate trends across past runs for the spec associated with run_id."""
    from app.database import get_mongo_db
    db = await get_mongo_db()
    run_repo = RunRepository(db)
    history_repo = ExecutionHistoryRepository(db)

    # 1. Fetch run doc to get spec_id
    run_doc = await run_repo.find_by_id(run_id)
    if not run_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run with id '{run_id}' not found."
        )

    spec_id = run_doc.get("spec_id", "unknown_spec")

    # 2. Get history documents for this spec_id
    history_docs = await history_repo.find_by_spec(spec_id)

    # 3. Calculate flaky endpoints
    flaky_endpoints = []
    run_totals = {}  # run_id -> {"pass_count": int, "total_executions": int, "executed_at": str}

    for doc in history_docs:
        op_id = doc.get("endpoint_operation_id")
        runs_history = doc.get("runs_history", [])

        # Sort runs_history chronologically by executed_at
        sorted_runs = sorted(runs_history, key=lambda r: r.get("executed_at", ""))
        
        # Calculate overall run totals across endpoints for trend
        for r in sorted_runs:
            r_id = r["run_id"]
            if r_id not in run_totals:
                run_totals[r_id] = {
                    "pass_count": 0,
                    "total_executions": 0,
                    "executed_at": r.get("executed_at", "")
                }
            run_totals[r_id]["pass_count"] += r.get("pass_count", 0)
            run_totals[r_id]["total_executions"] += r.get("total_executions", 0)

        # Retrieve last 10 runs for this specific endpoint
        last_10 = sorted_runs[-10:] if len(sorted_runs) > 10 else sorted_runs
        
        # Find last flaky timestamp
        last_flaky_at = None
        for r in reversed(sorted_runs):
            if r.get("flaky_count", 0) > 0:
                last_flaky_at = r.get("executed_at")
                break

        # Calculate last 10 runs flake rate
        total_executions_10 = sum(r.get("total_executions", 0) for r in last_10)
        flaky_count_10 = sum(r.get("flaky_count", 0) for r in last_10)
        flake_rate_10 = flaky_count_10 / total_executions_10 if total_executions_10 > 0 else 0.0

        # Endpoint is considered flaky if it has ever been flaky in history
        if doc.get("flaky_count", 0) > 0:
            flaky_endpoints.append({
                "endpoint_operation_id": op_id,
                "flake_rate_last_10_runs": flake_rate_10,
                "last_flaky_at": last_flaky_at
            })

    # 4. Calculate pass rate trend
    sorted_trend_runs = sorted(run_totals.items(), key=lambda item: item[1]["executed_at"])
    pass_rate_trend = []
    for r_id, val in sorted_trend_runs:
        total = val["total_executions"]
        p_rate = val["pass_count"] / total if total > 0 else 1.0
        pass_rate_trend.append({
            "run_id": r_id,
            "pass_rate": p_rate,
            "executed_at": val["executed_at"]
        })

    return {
        "spec_id": spec_id,
        "flaky_endpoints": flaky_endpoints,
        "pass_rate_trend": pass_rate_trend
    }
