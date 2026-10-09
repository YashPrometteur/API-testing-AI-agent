
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


def is_terminal_action(tc: TestCase) -> bool:
    """Identify terminal lifecycle actions that invalidate the active session (e.g. POST /logout, deactivate)."""
    req = tc.get("request", {})
    path = req.get("path", "").lower()
    op_id = tc.get("endpoint_operation_id", "").lower()
    return any(t in path or t in op_id for t in ("logout", "deactivate", "signout", "delete_account"))


def is_resource_creator(tc: TestCase) -> bool:
    """Identify if a test case creates a primary resource without requiring existing IDs (e.g. POST /create_subscription)."""
    if is_terminal_action(tc):
        return False
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


def get_onboarding_step_rank(tc: TestCase) -> int:
    """Return explicit rank for multi-step onboarding, CRUD lifecycle, and terminal actions."""
    if is_terminal_action(tc):
        return 999
    req = tc.get("request", {})
    path = req.get("path", "").lower()
    op_id = tc.get("endpoint_operation_id", "").lower()
    method = req.get("method", "GET").upper()
    
    # 1. Onboarding Guest Auth Steps (Must run first)
    if "request-otp" in path or "request_otp" in op_id:
        return 10
    elif "verify-otp" in path or "verify_otp" in op_id or "verify_primary_otp" in op_id:
        return 20
    elif "add-secondary" in path or "add_secondary" in op_id:
        return 30
    elif "verify-secondary" in path or "verify_secondary" in op_id:
        return 40
    elif "register" in path or "register" in op_id:
        return 50
    # 2. Master Data / Options Discovery (Dropdowns, lookup tables, options)
    elif ("options" in path or "options" in op_id) and method == "GET":
        return 55
    # 3. Post-Registration Profile & Preference Setup
    elif "preferences-update" in path or "preferences_update" in op_id or "preference" in path:
        return 60
    elif "profile-update" in path or "profile_update" in op_id:
        return 65
    # 4. Resource Uploads / Creators
    elif "upload" in path or "upload_images" in op_id or (method == "POST" and "image" in path):
        return 70
    elif "images" in path and method == "GET":
        return 75
    # 5. Entity Inspections & Updates
    elif "profile" in path and method == "GET":
        return 80
    elif "profile" in path and method in ("PATCH", "PUT"):
        return 85
    # 6. Resource Deletions
    elif "delete" in path or method == "DELETE":
        return 90
    return 100


def sort_functional_workflow_chain(test_cases: List[TestCase]) -> List[TestCase]:
    """Sort functional test cases in strict dependency order:
    1. Collection Discoverers (GET list)
    2. Multi-step Onboarding (request-otp -> verify-otp -> add-secondary -> verify-secondary -> register -> preferences -> profile)
    3. Generic Resource Creators (POST create)
    4. Dependents (Activate/Toggle -> GET details -> PATCH/PUT update)
    5. Deleters (DELETE)
    6. Terminal Actions (POST logout, deactivate) - ALWAYS LAST
    """
    discoverers = []
    onboarding = []
    creators = []
    dependents = []
    deleters = []
    terminal_actions = []
    others = []

    for tc in test_cases:
        if is_terminal_action(tc):
            terminal_actions.append(tc)
        elif is_resource_deleter(tc):
            deleters.append(tc)
        elif is_discovery_endpoint(tc):
            discoverers.append(tc)
        elif get_onboarding_step_rank(tc) < 100:
            onboarding.append(tc)
        elif is_resource_creator(tc):
            creators.append(tc)
        elif extract_resource_id_dependencies(tc):
            dependents.append(tc)
        else:
            others.append(tc)

    # Form canonical 1-to-1 sequential onboarding chain (1 testcase per step: rank 10 -> 20 -> 30 -> 40 -> 50 -> 60 -> 65)
    onboarding_map: Dict[int, TestCase] = {}
    extra_onboarding: List[TestCase] = []

    for tc in onboarding:
        rank = get_onboarding_step_rank(tc)
        if rank not in onboarding_map:
            onboarding_map[rank] = tc
        else:
            existing = onboarding_map[rank]
            is_new_full = "full" in str(tc.get("name", "")).lower() or tc.get("priority") == "high"
            is_exist_full = "full" in str(existing.get("name", "")).lower() or existing.get("priority") == "high"
            if is_new_full and not is_exist_full:
                extra_onboarding.append(existing)
                onboarding_map[rank] = tc
            else:
                extra_onboarding.append(tc)

    canonical_onboarding = [onboarding_map[r] for r in sorted(onboarding_map.keys())]

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

    return discoverers + canonical_onboarding + extra_onboarding + creators + dependents + others + deleters + terminal_actions


def sort_test_cases_by_dependency(test_cases: List[TestCase]) -> List[TestCase]:
    """Sort test cases by Track:
    Track 1: Functional / Happy-Path E2E Workflow Chain (Maintains session & resource state)
    Track 2: Negative, Boundary, Edge-Case, Security, and Auth Suites (Isolated Sandboxes)
    """
    functional_tcs = [tc for tc in test_cases if str(tc.get("category", "")).lower() == "functional"]
    non_functional_tcs = [tc for tc in test_cases if str(tc.get("category", "")).lower() != "functional"]

    ordered_functional = sort_functional_workflow_chain(functional_tcs)
    ordered_non_functional = sort_functional_workflow_chain(non_functional_tcs)

    # Track 1 (Happy Path) runs first, followed by Track 2 (Isolated Negatives)
    return ordered_functional + ordered_non_functional


async def execution_planner_node(state: TestRunState) -> Dict[str, Any]:
    """Execution Planner Node (Deterministic, no LLM call).

    Orders test cases by category track and dependency, batching them so that
    functional happy-path workflows maintain uninterrupted state progression
    while negative/boundary tests execute in isolated sandbox batches.
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

    # Separate into Track 1 (Functional) and Track 2 (Negative / Edge Case / Security)
    functional_tcs = [tc for tc in test_cases_to_plan if str(tc.get("category", "")).lower() == "functional"]
    non_functional_tcs = [tc for tc in test_cases_to_plan if str(tc.get("category", "")).lower() != "functional"]

    ordered_functional = sort_functional_workflow_chain(functional_tcs)
    ordered_non_functional = sort_functional_workflow_chain(non_functional_tcs)

    batch_size = DEFAULT_MAX_CONCURRENT
    batches = []

    # 1. Plan Track 1: Functional Happy-Path Batch (Sequential flow in 1 batch)
    if ordered_functional:
        batches.append({
            "batch_id": "batch_functional_1",
            "track": "functional",
            "test_cases": ordered_functional
        })

    # 2. Plan Track 2: Negative & Boundary Isolation Batches
    if ordered_non_functional:
        for i in range(0, len(ordered_non_functional), batch_size):
            batch_items = ordered_non_functional[i : i + batch_size]
            batches.append({
                "batch_id": f"batch_isolated_{len(batches) + 1}",
                "track": "negative_isolated",
                "test_cases": batch_items
            })

    logger.info(
        f"[EXECUTION_PLANNER] Created {len(batches)} batches "
        f"({len(functional_tcs)} functional, {len(non_functional_tcs)} negative/isolated) "
        f"for operations {list(unique_ops)}."
    )

    return {
        "execution_batches": batches,
        "status": "running"
    }
