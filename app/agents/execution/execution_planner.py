
import logging
import re
from typing import Any, Dict, List
from app.graph.state import TestCase, TestRunState

logger = logging.getLogger("execution_planner")

DEFAULT_MAX_CONCURRENT = 5


def extract_resource_id_dependencies(tc: TestCase) -> set[str]:
    """Extract referenced resource identifiers (e.g. 'petId', 'orderId', 'userId') from test case."""
    req = tc.get("request", {})
    path = req.get("path", "")
    path_params = req.get("path_params", {})
    query_params = req.get("query_params", {})
    body = req.get("body") if isinstance(req.get("body"), dict) else {}

    refs = set()

    # Path template braces like /pet/{petId}
    matches = re.findall(r"\{([^}]+)\}", path)
    for m in matches:
        refs.add(m)

    for k in path_params.keys():
        refs.add(k)

    for k, v in query_params.items():
        if "id" in k.lower():
            refs.add(k)

    for k, v in body.items():
        if "id" in k.lower() and k != "id":
            refs.add(k)

    return refs


def is_resource_creator(tc: TestCase) -> bool:
    """Identify if a test case creates a primary resource (e.g., POST request with valid body)."""
    req = tc.get("request", {})
    method = req.get("method", "GET").upper()
    cat = tc.get("category", "")
    return method == "POST" and cat in ("functional", "edge_case")


def sort_test_cases_by_dependency(test_cases: List[TestCase]) -> List[TestCase]:
    """Sort test cases so resource-creating POST requests execute before dependent GET/PUT/DELETE requests."""
    creators = []
    dependents = []
    others = []

    for tc in test_cases:
        if is_resource_creator(tc):
            creators.append(tc)
        elif extract_resource_id_dependencies(tc):
            dependents.append(tc)
        else:
            others.append(tc)

    # Topological order: Creators first, then others, then dependents
    return creators + others + dependents


async def execution_planner_node(state: TestRunState) -> Dict[str, Any]:
    """Execution Planner Node (Deterministic, no LLM call).

    Orders test cases by dependency and batches them according to host rate limits.
    During reflection re-execution cycles, filters target test cases to only failed/errored cases.
    """
    test_cases = state.get("test_cases", [])
    execution_results = state.get("execution_results", [])
    reflection_count = state.get("reflection_count", 0)
    route_decision = state.get("route_decision")

    # Filter to failed/errored test cases only during reflection re-execution cycles
    if (reflection_count > 0 or route_decision == "loop_back") and execution_results:
        failed_tc_ids = {
            res["test_case_id"]
            for res in execution_results
            if isinstance(res, dict) and res.get("status") in ("fail", "error")
        }
        test_cases_to_plan = [tc for tc in test_cases if isinstance(tc, dict) and tc.get("id") in failed_tc_ids]
        logger.info(
            f"[EXECUTION_PLANNER] Reflection cycle ({reflection_count}): "
            f"Filtering execution to {len(test_cases_to_plan)} failed test case(s) out of {len(test_cases)} total."
        )
    else:
        test_cases_to_plan = [tc for tc in test_cases if isinstance(tc, dict)]

    unique_ops = set(tc.get("endpoint_operation_id") for tc in test_cases_to_plan if isinstance(tc, dict))
    logger.info(f"[EXECUTION_PLANNER] Planning execution for {len(test_cases_to_plan)} test cases across operations: {list(unique_ops)}")

    if not test_cases_to_plan:
        logger.warning("[EXECUTION_PLANNER] No test cases to execute!")
        return {
            "execution_batches": [],
            "status": "running"
        }

    # 1. Sort test cases with creator-before-dependent dependency ordering
    ordered_test_cases = sort_test_cases_by_dependency(test_cases_to_plan)

    # 2. Group into execution batches based on DEFAULT_MAX_CONCURRENT limit
    batch_size = DEFAULT_MAX_CONCURRENT
    batches = []
    for i in range(0, len(ordered_test_cases), batch_size):
        batch_items = ordered_test_cases[i : i + batch_size]
        batch_dict = {
            "batch_id": f"batch_{len(batches) + 1}",
            "test_cases": batch_items
        }
        batches.append(batch_dict)

    logger.info(
        f"[EXECUTION_PLANNER] Successfully created {len(batches)} execution batches "
        f"for operations {list(unique_ops)} (max {batch_size} cases per batch)."
    )

    return {
        "execution_batches": batches,
        "status": "running"
    }
