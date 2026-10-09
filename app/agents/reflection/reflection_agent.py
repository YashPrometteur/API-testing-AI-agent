import logging
from typing import Any, Dict, List

from app.graph.state import Finding, TestRunState, ExecutionResult

logger = logging.getLogger("reflection_agent")


async def reflection_agent_node(state: TestRunState) -> Dict[str, Any]:
    """Reflection Agent Node: Implements transient failure loop-back and flaky detection."""
    logger.info("[REFLECTION_AGENT] Analyzing findings and execution history...")

    findings: List[Finding] = state.get("findings", [])
    execution_results: List[ExecutionResult] = state.get("execution_results", [])
    test_cases = {tc["id"]: tc for tc in state.get("test_cases", [])}
    reflection_count = state.get("reflection_count", 0)

    # 1. Detect and mark Flaky tests
    # Flaky should ONLY be assigned when the result differs across multiple attempts (>= 2) in the current run.
    results_by_tc: Dict[str, List[ExecutionResult]] = {}
    for res in execution_results:
        tc_id = res["test_case_id"]
        results_by_tc.setdefault(tc_id, []).append(res)

    updated_execution_results = list(execution_results)
    for tc_id, runs in results_by_tc.items():
        history_statuses = []
        for r in runs:
            if r.get("history"):
                history_statuses.extend([h.get("status", "").lower() for h in r["history"]])
            else:
                history_statuses.append(r.get("status", "").lower())

        total_attempts = len(history_statuses)
        has_pass = any(s == "pass" for s in history_statuses)
        has_fail = any(s in ("fail", "error") for s in history_statuses)

        if total_attempts >= 2 and has_pass and has_fail:
            logger.info(f"[REFLECTION_AGENT] Flaky test case detected: '{tc_id}' ({total_attempts} attempts). Marking as flaky.")
            for res in updated_execution_results:
                if res["test_case_id"] == tc_id:
                    res["status"] = "flaky"
        elif total_attempts <= 1:
            # Single attempt can NEVER be flaky
            for res in updated_execution_results:
                if res["test_case_id"] == tc_id and res.get("status") == "flaky":
                    single_s = history_statuses[0] if history_statuses else "pass"
                    res["status"] = single_s if single_s in ("pass", "fail", "error") else "pass"

    # 2. Reflection loop decision logic
    loop_back = False
    options = state.get("options", {}) or {}
    from app.config.settings import settings
    max_reflection_loops = options.get("max_reflection_loops", getattr(settings, "MAX_REFLECTION_LOOPS", 2))

    try:
        if reflection_count < max_reflection_loops:
            # Primary check: Re-execute failed test cases that have only executed a single attempt (attempt_count <= 1)
            for res in updated_execution_results:
                if res.get("status") in ("fail", "error") and res.get("attempt_count", 1) <= 1:
                    tc_id = res["test_case_id"]
                    logger.info(
                        f"[REFLECTION_AGENT] Test case '{tc_id}' failed on initial attempt (attempt_count <= 1). "
                        f"Requesting re-execution loop ({reflection_count + 1}/{max_reflection_loops})."
                    )
                    loop_back = True
                    break

            if not loop_back:
                # Secondary check: Low-confidence finding check (< 0.5)
                for finding in findings:
                    if finding.get("confidence", 0.0) < 0.5:
                        tc_id = finding["test_case_id"]
                        tc = test_cases.get(tc_id, {})
                        op_id = tc.get("endpoint_operation_id")
                        runs = results_by_tc.get(tc_id, [])
                        latest_run = runs[-1] if runs else {}
                        single_attempt = latest_run.get("attempt_count", 0) <= 1

                        endpoint_runs = [
                            r for r in execution_results
                            if test_cases.get(r["test_case_id"], {}).get("endpoint_operation_id") == op_id
                        ]
                        failed_endpoint_runs = [
                            r for r in endpoint_runs
                            if r.get("status") in ("fail", "error")
                        ]
                        no_prior_history = len(failed_endpoint_runs) <= 1

                        if single_attempt and no_prior_history:
                            logger.info(
                                f"[REFLECTION_AGENT] Finding for '{tc_id}' (endpoint '{op_id}') has low confidence ({finding.get('confidence')}) "
                                f"and appears transient. Requesting re-execution loop (loop count: {reflection_count + 1}/{max_reflection_loops})."
                            )
                            loop_back = True
                            break
    except Exception as e:
        logger.error(f"[REFLECTION_AGENT] Error in reflection decision logic: {e}. Defaulting to proceed forward.", exc_info=True)
        loop_back = False

    if loop_back:
        return {
            "execution_results": updated_execution_results,
            "reflection_count": reflection_count + 1,
            "route_decision": "loop_back",
            "status": "running"
        }
    else:
        logger.info("[REFLECTION_AGENT] All findings evaluated. Proceeding forward to Guardrails.")
        return {
            "execution_results": updated_execution_results,
            "route_decision": "proceed_to_guardrail",
            "status": "running"
        }
