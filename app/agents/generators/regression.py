import logging
from typing import Any, Dict, Optional
from motor.motor_asyncio import AsyncIOMotorClient

from app.config.settings import settings
from app.middleware.logging_middleware import ctx_run_id
from app.models.test_case import TestCaseGeneratedModel
from app.repositories.test_case_repository import TestCaseRepository
from app.utils.ids import generate_test_case_id

logger = logging.getLogger("regression_generator")


async def regression_generator_node(state_or_payload: Dict[str, Any], db_override: Optional[Any] = None) -> Dict[str, Any]:
    """Regression test case generator node (deterministic database query, no LLM call)."""
    run_id = state_or_payload.get("run_id", "")
    if run_id:
        ctx_run_id.set(run_id)

    spec_id = state_or_payload.get("spec_id")
    logger.info(f"[REGRESSION_GENERATOR] Querying prior test cases from MongoDB for spec_id='{spec_id}'...")

    try:
        if db_override is not None:
            db = db_override
        else:
            from app.database import get_mongo_db
            db = await get_mongo_db()

        repo = TestCaseRepository(db)
        prior_cases_docs = await repo.get_regression_test_cases(spec_id=spec_id, limit=50)

        # Build target operation_ids and paths from current state endpoints
        target_endpoints = state_or_payload.get("endpoints", [])
        target_op_ids = {ep.get("operation_id", "").lower() for ep in target_endpoints if ep.get("operation_id")}
        target_paths = {ep.get("path", "").lower() for ep in target_endpoints if ep.get("path")}

        formatted_test_cases = []
        for doc in prior_cases_docs:
            op_id = (doc.get("endpoint_operation_id") or "").lower()
            req_path = (doc.get("request", {}).get("path") or "").lower()

            # Filter out historical test cases for unselected endpoints
            if target_op_ids or target_paths:
                if op_id not in target_op_ids and req_path not in target_paths:
                    continue

            raw_id = doc.get("_id") or doc.get("id") or generate_test_case_id("regression")
            tc_dict = {
                "id": str(raw_id),
                "endpoint_operation_id": doc.get("endpoint_operation_id", ""),
                "category": "regression",
                "name": doc.get("name", "Prior Regression Test"),
                "request": doc.get("request", {"method": "GET", "path": "/"}),
                "expected": doc.get("expected", {"status_code": 200, "response_schema_match": True}),
                "priority": doc.get("priority", "high"),
                "generated_by": "regression_generator_v1",
                "rationale": f"Loaded from prior run history (flake_rate={doc.get('execution_history', {}).get('flake_rate', 0.0)})"
            }
            # Validate strict schema conformance
            full_model = TestCaseGeneratedModel(**tc_dict)
            formatted_test_cases.append(full_model.model_dump())

        logger.info(f"[REGRESSION_GENERATOR] Total raw historical cases retrieved: {len(prior_cases_docs)}. Filtered down to {len(formatted_test_cases)} matching state endpoints: {[tc.get('endpoint_operation_id') for tc in formatted_test_cases]}")
        return {"test_cases": formatted_test_cases}

    except Exception as e:
        logger.warning(f"[REGRESSION_GENERATOR] Failed to query historical regression test cases: {e}. Skipping.")
        return {"test_cases": []}
