import logging
from typing import List, Union
from langgraph.types import Send
from app.graph.state import TestRunState

logger = logging.getLogger("graph_routers")


def fan_out_generators(state: TestRunState) -> List[Send]:
    """Fan-out router: Inspects test_plan and returns Send() calls for active generator categories."""
    test_plan = state.get("test_plan", {}).get("plan", {})
    active_categories = set()

    for op_id, op_plan in test_plan.items():
        for cat in op_plan.get("categories", []):
            active_categories.add(cat.lower())

    # Map category names to graph node names
    category_node_map = {
        "functional": "generate_functional",
        "negative": "generate_negative",
        "edge_case": "generate_edge_case",
        "security": "generate_security",
        "auth": "generate_auth",
        "authz": "generate_authz",
        "regression": "generate_regression",
    }

    sends = []
    payload = {
        "run_id": state.get("run_id", ""),
        "endpoints": state.get("endpoints", []),
        "test_plan": state.get("test_plan", {}),
        "spec_id": state.get("spec_id", "")
    }

    for cat, node_name in category_node_map.items():
        if cat in active_categories or not active_categories:
            sends.append(Send(node_name, payload))

    logger.info(f"[FAN_OUT_GENERATORS] Spawning {len(sends)} generator branch nodes: {[s.node for s in sends]}")
    return sends


def join_test_cases_router(state: TestRunState) -> str:
    """Join decision router: Checks total generated test cases."""
    test_cases = state.get("test_cases", [])
    if not test_cases or len(test_cases) == 0:
        logger.error("[JOIN_TEST_CASES_ROUTER] Zero test cases collected across all generator branches! Routing to fail_run.")
        return "fail_run"
    
    logger.info(f"[JOIN_TEST_CASES_ROUTER] Successfully collected {len(test_cases)} test cases. Routing to proceed_to_execution_planning.")
    return "proceed_to_execution_planning"


def fan_out_execution_batches(state: TestRunState) -> List[Send]:
    """Fan-out router: Spawns execute_batch node in parallel for each execution batch."""
    batches = state.get("execution_batches", [])
    target_base_url = state.get("target_base_url", "https://petstore.swagger.io/v2")

    sends = []
    run_id = state.get("run_id", "")
    for batch in batches:
        payload = {
            "run_id": run_id,
            "batch_id": batch.get("batch_id", "batch_1"),
            "test_cases": batch.get("test_cases", []),
            "target_base_url": target_base_url
        }
        sends.append(Send("execute_batch", payload))

    logger.info(f"[FAN_OUT_EXECUTION_BATCHES] Spawning {len(sends)} batch execution nodes.")
    return sends


def validation_router(state: TestRunState) -> str:
    """Pure routing function: Checks execution_results pass/fail verdicts."""
    execution_results = state.get("execution_results", [])
    if not execution_results:
        logger.warning("[VALIDATION_ROUTER] No execution results found! Routing to proceed_to_reflection.")
        return "proceed_to_reflection"

    has_failures = any(res.get("status") in ("fail", "error", "flaky") for res in execution_results)

    if has_failures:
        logger.info("[VALIDATION_ROUTER] Defect/Error/Flaky detected in execution results. Routing to proceed_to_reflection.")
        return "proceed_to_reflection"

    logger.info("[VALIDATION_ROUTER] All test cases passed successfully! Routing to proceed_to_report.")
    return "proceed_to_report"


def reflection_router(state: TestRunState) -> str:
    """Conditional router following reflection_agent: decides whether to re-execute or proceed."""
    decision = state.get("route_decision")
    if decision == "loop_back":
        logger.info("[REFLECTION_ROUTER] Low-confidence/transient failures detected. Routing back to execution_planner.")
        return "loop_back_to_execution"

    logger.info("[REFLECTION_ROUTER] Proceeding forward to Guardrails/Report.")
    return "proceed_to_guardrail"
