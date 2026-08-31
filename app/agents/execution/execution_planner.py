
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

    # Direct 24-character hexadecimal MongoDB ObjectId in path e.g. /64b8f0e9e5162e84ea981e01
    if re.search(r"/[0-9a-fA-F]{24}", path) or re.search(r"/(?:valid_plan_id|some_plan_id|12345|123)", path):
        refs.add("primary_id")

    for k in path_params.keys():
        refs.add(k)

    for k, v in query_params.items():
        if "id" in k.lower():
            refs.add(k)

    for k, v in body.items():
        if "id" in k.lower() and k != "id":
            refs.add(k)

    return refs


def is_discovery_endpoint(tc: TestCase) -> bool:
    """Identify if a test case queries resource collections (e.g. GET /plans_list) that discover existing IDs."""
    req = tc.get("request", {})
    path = req.get("path", "").lower()
    op_id = tc.get("endpoint_operation_id", "").lower()
    method = req.get("method", "GET").upper()
    cat = tc.get("category", "")
    has_id_deps = bool(extract_resource_id_dependencies(tc))
    
    # Must be a collection-level GET without resource IDs (e.g. /get_plans_list)
    is_collection = "list" in path or "list" in op_id or "search" in path or "all" in op_id
    return method == "GET" and not has_id_deps and is_collection and cat in ("functional", "edge_case")


def is_resource_creator(tc: TestCase) -> bool:
    """Identify if a test case creates a primary resource without requiring existing IDs (e.g. POST /create_subscription)."""
    req = tc.get("request", {})
    path = req.get("path", "").lower()
    op_id = tc.get("endpoint_operation_id", "").lower()
    method = req.get("method", "GET").upper()
    cat = tc.get("category", "")
    has_id_deps = bool(extract_resource_id_dependencies(tc))
    
    is_create = ("create" in path or "create" in op_id or "add" in op_id or "new" in path)
    return method == "POST" and not has_id_deps and (is_create or cat in ("functional", "edge_case"))


def is_resource_deleter(tc: TestCase) -> bool:
    """Identify if a test case deletes/destroys a resource (e.g., DELETE request)."""
    req = tc.get("request", {})
    method = req.get("method", "GET").upper()
    return method == "DELETE"


def sort_test_cases_by_dependency(test_cases: List[TestCase]) -> List[TestCase]:
    """Sort test cases: Discoverers -> Creators -> Dependents (Toggle -> Read -> Update) -> Others -> Deleters (DELETE)."""
    discoverers = []
    creators = []
    dependents = []
    deleters = []
    others = []

    for tc in test_cases:
        if is_resource_deleter(tc):
            deleters.append(tc)
        elif is_discovery_endpoint(tc):
            discoverers.append(tc)
        elif is_resource_creator(tc):
            creators.append(tc)
        elif extract_resource_id_dependencies(tc):
            dependents.append(tc)
        else:
            others.append(tc)

    # Sub-sort dependents: Toggles (activate) -> GET details (read) -> PATCH updates (modify)
    def dependent_sort_key(tc):
        method = tc.get("request", {}).get("method", "GET").upper()
        path = tc.get("request", {}).get("path", "").lower()
        if "toggle" in path or (method == "POST" and "status" in path):
            return 1  # Activate first
        elif method == "GET":
            return 2  # Read active plan second
        elif method in ("PATCH", "PUT"):
            return 3  # Update plan third
        return 4

    dependents.sort(key=dependent_sort_key)

    # Topological order:
    # 1. Discoverers (GET list)
    # 2. Creators (POST create)
    # 3. Dependents (POST toggle -> GET details -> PATCH update)
    # 4. Others (Authz / standalone edge cases)
    # 5. Deleters (DELETE)
    return discoverers + creators + dependents + others + deleters


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
