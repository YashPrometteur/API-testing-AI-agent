import logging
import os
from typing import Any, Dict, List, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field

from app.config.settings import settings
from app.graph.state import TestRunState, ExecutionResult, Finding
from app.repositories.execution_history_repository import ExecutionHistoryRepository
from app.llm import get_llm_client
from app.tools.report_renderer import ReportRendererTool
from app.utils.datetime import current_utc_iso
from bson.binary import Binary

logger = logging.getLogger("reporting_agent")


class ReportSummary(BaseModel):
    summary_text: str = Field(description="A clean, executive-level narrative paragraph summarizing the API test run results.")


async def reporting_agent_node(state: TestRunState, db_override: Optional[Any] = None) -> Dict[str, Any]:
    """Reporting Agent: Summarizes executions, updates history metrics, calls LLM for summary, and renders HTML/PDF."""
    logger.info("[REPORTING_AGENT] Starting report generation...")

    run_id = state.get("run_id")
    execution_results: List[ExecutionResult] = state.get("execution_results", [])
    findings: List[Finding] = state.get("findings", [])
    test_cases_list = state.get("test_cases", [])
    test_cases = {tc["id"]: tc for tc in test_cases_list}

    if db_override is not None:
        db = db_override
    else:
        from app.database import get_mongo_db
        db = await get_mongo_db()
    history_repo = ExecutionHistoryRepository(db)

    # 1. Load run document to get spec_id and current run status
    spec_id = "unknown_spec"
    run_status = "completed"
    try:
        run_doc = await db["runs"].find_one({"_id": run_id})
        if run_doc:
            spec_id = run_doc.get("spec_id", "unknown_spec")
            run_status = run_doc.get("status", "completed")
    except Exception as e:
        logger.warning(f"[REPORTING_AGENT] Failed to fetch run document for spec_id: {e}")

    # Fetch defects created in this run (only real def_ IDs)
    defects_created = []
    try:
        cursor = db["defects"].find({"run_id": run_id})
        defects = await cursor.to_list(length=100)
        defects_created = [d["_id"] for d in defects if isinstance(d, dict) and d.get("_id")]
    except Exception as e:
        logger.warning(f"[REPORTING_AGENT] Failed to fetch defects: {e}")

    # 1b. Fetch authoritative findings from MongoDB to ensure latest guardrail_status post-human-review
    refreshed_findings = []
    for f in findings:
        f_dict = dict(f) if isinstance(f, dict) else {}
        fid = f_dict.get("id") or f_dict.get("_id")
        if fid:
            try:
                db_finding = await db["findings"].find_one({"_id": fid})
                if db_finding:
                    state_status = f_dict.get("guardrail_status")
                    db_status = db_finding.get("guardrail_status")
                    if state_status and db_status and state_status != db_status:
                        logger.info(
                            f"[REPORTING_AGENT] Finding '{fid}' guardrail_status refreshed from DB: "
                            f"'{state_status}' -> '{db_status}' for final report."
                        )
                    f_dict.update({
                        "guardrail_status": db_status or state_status,
                        "reviewer": db_finding.get("reviewer", f_dict.get("reviewer")),
                        "review_notes": db_finding.get("review_notes", f_dict.get("review_notes")),
                        "reviewed_at": db_finding.get("reviewed_at", f_dict.get("reviewed_at")),
                        "severity": db_finding.get("severity", f_dict.get("severity")),
                        "confidence": db_finding.get("confidence", f_dict.get("confidence")),
                        "is_duplicate": db_finding.get("is_duplicate", f_dict.get("is_duplicate")),
                        "duplicate_of": db_finding.get("duplicate_of", f_dict.get("duplicate_of")),
                    })
            except Exception as e:
                logger.warning(f"[REPORTING_AGENT] Failed to refresh finding '{fid}' from DB: {e}")
        refreshed_findings.append(f_dict)
    findings = refreshed_findings

    # 2. Perform deterministic aggregation
    total_executions = len(execution_results)
    pass_count = 0
    fail_count = 0
    flaky_count = 0
    not_executed_count = 0

    categories_metrics: Dict[str, Dict[str, int]] = {}
    endpoint_run_metrics: Dict[str, Dict[str, int]] = {}

    for res in execution_results:
        tc_id = res["test_case_id"]
        tc = test_cases.get(tc_id, {})
        category = tc.get("category", "functional")
        op_id = tc.get("endpoint_operation_id", "unknown_operation")

        # Init category dict
        if category not in categories_metrics:
            categories_metrics[category] = {"pass_count": 0, "fail_count": 0, "flaky_count": 0, "not_executed_count": 0}
        # Init endpoint dict
        if op_id not in endpoint_run_metrics:
            endpoint_run_metrics[op_id] = {"pass_count": 0, "fail_count": 0, "flaky_count": 0, "not_executed_count": 0, "total_executions": 0}

        status = res.get("status", "fail")
        endpoint_run_metrics[op_id]["total_executions"] += 1

        if status == "pass":
            pass_count += 1
            categories_metrics[category]["pass_count"] += 1
            endpoint_run_metrics[op_id]["pass_count"] += 1
        elif status == "flaky":
            flaky_count += 1
            categories_metrics[category]["flaky_count"] += 1
            endpoint_run_metrics[op_id]["flaky_count"] += 1
        elif status == "not_executed":
            not_executed_count += 1
            categories_metrics[category]["not_executed_count"] += 1
            endpoint_run_metrics[op_id]["not_executed_count"] += 1
        else:  # fail or error
            fail_count += 1
            categories_metrics[category]["fail_count"] += 1
            endpoint_run_metrics[op_id]["fail_count"] += 1

    pass_rate = pass_count / total_executions if total_executions > 0 else 1.0

    # Severity findings count
    severity_findings: Dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f.get("severity", "medium")
        if sev in severity_findings:
            severity_findings[sev] += 1

    # Flaky endpoints in this run
    flaky_endpoints = [op_id for op_id, m in endpoint_run_metrics.items() if m["flaky_count"] > 0]

    # Save to execution history incrementally
    for op_id, m in endpoint_run_metrics.items():
        try:
            await history_repo.update_incremental(
                spec_id=spec_id,
                endpoint_operation_id=op_id,
                run_id=run_id,
                pass_count=m["pass_count"],
                fail_count=m["fail_count"],
                flaky_count=m["flaky_count"],
                total_executions=m["total_executions"]
            )
        except Exception as e:
            logger.error(f"[REPORTING_AGENT] Failed to update execution history for endpoint '{op_id}': {e}")

    # 3. LLM Call for Executive Summary
    summary_text = ""
    if total_executions > 0:
        llm_client = get_llm_client()
        system_prompt = (
            "You are a Senior QA Director summarizing test execution results for the executive leadership team. "
            "Provide a concise, professional executive summary paragraph (3-4 sentences) outlining the test run outcomes, "
            "highlighting any critical vulnerabilities/defects found, flaky endpoints, or overall API stability."
        )
        user_prompt = (
            f"API Spec ID: {spec_id}\n"
            f"Run ID: {run_id}\n"
            f"Total Test Cases Executed: {total_executions}\n"
            f"Passed: {pass_count} | Failed: {fail_count} | Flaky: {flaky_count}\n"
            f"Overall Pass Rate: {pass_rate * 100:.2f}%\n"
            f"Flaky Endpoints Identified: {', '.join(flaky_endpoints) if flaky_endpoints else 'None'}\n"
            f"Critical/High Severity Findings: {severity_findings.get('critical', 0)} critical, {severity_findings.get('high', 0)} high.\n"
            f"Defects Created: {', '.join(defects_created) if defects_created else 'None'}\n"
        )

        for attempt in range(2):
            try:
                res = await llm_client.generate_structured(
                    prompt=user_prompt,
                    response_model=ReportSummary,
                    system_prompt=system_prompt,
                    temperature=0.3
                )
                summary_text = res.summary_text
                break
            except Exception as e:
                logger.warning(f"[REPORTING_AGENT] Executive summary LLM call failed (attempt {attempt + 1}/2): {e}")

        # Deterministic fallback summary if LLM summary failed or returned empty
        if not summary_text:
            flaky_str = f" Flaky endpoints identified: {', '.join(flaky_endpoints)}." if flaky_endpoints else ""
            summary_text = (
                f"Automated test execution completed for run '{run_id}'. "
                f"Out of {total_executions} total test cases executed across {len(endpoint_run_metrics)} operations, "
                f"{pass_count} passed, {fail_count} failed, and {flaky_count} exhibited flakiness, "
                f"yielding an overall pass rate of {pass_rate * 100:.1f}%.{flaky_str}"
            )

    # 4. Construct JSON Report (suppressing resolved findings for test cases whose final attempt succeeded)
    final_success_tc_ids = set()
    for res in execution_results:
        if not isinstance(res, dict):
            continue
        tc_id = res.get("test_case_id")
        if not tc_id:
            continue
        stat = res.get("status")
        hist = res.get("history") or []
        last_hist_stat = hist[-1].get("status") if hist else None

        if stat == "pass":
            final_success_tc_ids.add(tc_id)
        elif stat == "flaky" and last_hist_stat == "PASS":
            final_success_tc_ids.add(tc_id)
        elif not res.get("assertion_failures") and res.get("actual_status_code") in (200, 201, 204):
            final_success_tc_ids.add(tc_id)

    active_findings = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        tc_id = f.get("test_case_id")
        if tc_id and tc_id in final_success_tc_ids:
            logger.info(
                f"[REPORTING_AGENT] Suppressing resolved finding '{f.get('id')}' for test case '{tc_id}' "
                f"whose final attempt succeeded."
            )
            continue
        active_findings.append(f)

    unique_findings = []
    seen_ids = set()
    for f in active_findings:
        fid = f.get("id") or f.get("_id")
        if fid:
            if fid not in seen_ids:
                seen_ids.add(fid)
                unique_findings.append(f)
        else:
            unique_findings.append(f)

    endpoints_list = state.get("endpoints", [])
    ep_map = {ep.get("operation_id"): ep for ep in endpoints_list if isinstance(ep, dict) and ep.get("operation_id")}

    endpoints_summary: Dict[str, Dict[str, Any]] = {}
    for op_id, m in endpoint_run_metrics.items():
        ep = ep_map.get(op_id, {})
        tot = m.get("total_executions", 0)
        p_count = m.get("pass_count", 0)
        p_rate = (p_count / tot) if tot > 0 else 1.0
        endpoints_summary[op_id] = {
            "method": (ep.get("method") or "GET").upper(),
            "path": ep.get("path") or "/",
            "pass_count": p_count,
            "fail_count": m.get("fail_count", 0),
            "flaky_count": m.get("flaky_count", 0),
            "not_executed_count": m.get("not_executed_count", 0),
            "pass_rate": round(p_rate, 4)
        }

    report_dict = {
        "run_id": run_id,
        "status": "completed",
        "summary": {
            "total_executions": total_executions,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "flaky_count": flaky_count,
            "not_executed_count": not_executed_count if 'not_executed_count' in locals() else 0,
            "pass_rate": pass_rate,
            "endpoints": endpoints_summary,
            "categories": categories_metrics,
            "severity_findings": severity_findings,
            "flaky_endpoints_detected": flaky_endpoints,
            "defects_created": defects_created
        },
        "executive_summary": summary_text,
        "findings": unique_findings,
        "created_at": current_utc_iso()
    }

    # 5. Build detailed test_case_executions list for debugging & MongoDB inspection
    endpoints_list = state.get("endpoints", [])
    ep_map = {ep.get("operation_id"): ep for ep in endpoints_list if isinstance(ep, dict) and ep.get("operation_id")}
    execution_results_map = {res["test_case_id"]: res for res in execution_results if isinstance(res, dict) and res.get("test_case_id")}
    findings_map = {f.get("test_case_id"): f for f in active_findings if isinstance(f, dict) and f.get("test_case_id")}
    retry_counts = state.get("retry_counts", {})

    priority_weight = {"critical": 1, "high": 2, "medium": 3, "low": 4}

    from app.agents.execution.execution_planner import sort_test_cases_by_dependency
    sorted_report_tcs = sort_test_cases_by_dependency([tc for tc in test_cases_list if isinstance(tc, dict)])

    unique_persisted_ops = set(tc.get("endpoint_operation_id") for tc in sorted_report_tcs if isinstance(tc, dict))
    logger.info(f"[REPORTING_AGENT] Assembling {len(sorted_report_tcs)} test_case_executions for operations: {list(unique_persisted_ops)}")

    test_case_executions: List[Dict[str, Any]] = []
    for tc in sorted_report_tcs:
        if not isinstance(tc, dict):
            continue
        tc_id = tc.get("id", "")
        op_id = tc.get("endpoint_operation_id", "")
        ep = ep_map.get(op_id, {})

        req = tc.get("request", {}) if isinstance(tc.get("request"), dict) else {}
        path = req.get("path") or ep.get("path", "")
        method = (req.get("method") or ep.get("method", "GET")).upper()
        endpoint_str = f"{method} {path}"

        category = tc.get("category", "functional")
        priority = tc.get("priority", "medium")
        generated_by = tc.get("generated_by", "")

        tc_entry = {
            "id": tc_id,
            "endpoint_operation_id": op_id,
            "path": path,
            "method": method,
            "category": category,
            "name": tc.get("name", ""),
            "generated_by": generated_by,
            "priority": priority,
            "rationale": tc.get("rationale", ""),
            "request": req,
            "expected": tc.get("expected", {})
        }

        exec_res = execution_results_map.get(tc_id)
        exec_entry = None
        execution_status_str = "UNKNOWN"

        if exec_res:
            raw_status = exec_res.get("status", "unknown")
            execution_status_str = raw_status.upper()
            failures = exec_res.get("assertion_failures", [])

            # Generate smart human-readable failure_reason if missing
            failure_reason = exec_res.get("failure_reason") or exec_res.get("error")
            if not failure_reason and failures:
                exp_code = tc.get("expected", {}).get("status_code")
                act_code = exec_res.get("actual_status_code")
                if exp_code and act_code and exp_code != act_code:
                    failure_reason = f"Expected HTTP {exp_code} but received HTTP {act_code}."
                else:
                    failure_reason = ", ".join([str(f) for f in failures])

            exec_entry = {
                "status": raw_status,
                "actual_status_code": exec_res.get("actual_status_code"),
                "actual_body": exec_res.get("actual_body"),
                "actual_response": exec_res.get("actual_response") or exec_res.get("actual_body"),
                "actual_headers": exec_res.get("actual_headers", {}),
                "latency_ms": float(exec_res.get("latency_ms", 0.0)),
                "schema_valid": bool(exec_res.get("schema_valid", True)),
                "assertion_failures": failures,
                "attempt_count": int(exec_res.get("attempt_count", 1)),
                "executed_at": exec_res.get("executed_at") or current_utc_iso(),
                "failure_reason": failure_reason
            }

        # Build retry history from actual recorded attempts
        history = list(exec_res.get("history") or []) if exec_res else []
        if not history and exec_res:
            attempts_cnt = int(exec_res.get("attempt_count", 1))
            if attempts_cnt > 1:
                history = [
                    {
                        "attempt": i + 1,
                        "status": "FAIL" if i < attempts_cnt - 1 else execution_status_str,
                        "status_code": exec_res.get("actual_status_code"),
                        "latency_ms": float(exec_res.get("latency_ms", 0.0)),
                        "assertion_failures": list(exec_res.get("assertion_failures") or []),
                        "executed_at": exec_res.get("executed_at") or current_utc_iso()
                    }
                    for i in range(attempts_cnt)
                ]
            else:
                history = [{
                    "attempt": 1,
                    "status": execution_status_str,
                    "status_code": exec_res.get("actual_status_code"),
                    "latency_ms": float(exec_res.get("latency_ms", 0.0)),
                    "assertion_failures": list(exec_res.get("assertion_failures") or []),
                    "executed_at": exec_res.get("executed_at") or current_utc_iso()
                }]

        # Bug 2 fix: If only 1 attempt exists in history, status CANNOT be FLAKY
        if len(history) <= 1 and execution_status_str == "FLAKY":
            single_stat = history[0]["status"] if history else "PASS"
            execution_status_str = single_stat.upper()
            if exec_entry:
                exec_entry["status"] = execution_status_str.lower()

        # Bug 1 fix: A test case whose final attempt succeeded must NEVER carry an attached defect finding
        finding_info = None
        if execution_status_str != "PASS" and tc_id not in final_success_tc_ids:
            raw_finding = findings_map.get(tc_id)
            if isinstance(raw_finding, dict):
                finding_info = {**raw_finding}

        entry_record = {
            "test_case_id": tc_id,
            "endpoint_operation_id": op_id,
            "endpoint_path": path,
            "http_method": method,
            "endpoint": endpoint_str,
            "category": category,
            "execution_status": execution_status_str,
            "generated_by": generated_by,
            "priority": priority,
            "test_case": tc_entry,
            "execution_result": exec_entry,
            "finding": finding_info,
            "history": history
        }

        test_case_executions.append(entry_record)

    # Sort test_case_executions deterministically: Category Track (Functional -> Negative) -> Onboarding Sequence -> Endpoint -> Priority -> Name
    category_rank = {
        "functional": 0,
        "negative": 1,
        "edge_case": 2,
        "security": 3,
        "auth": 4,
        "authz": 5,
        "regression": 6
    }
    from app.agents.execution.execution_planner import get_onboarding_step_rank
    test_case_executions.sort(
        key=lambda x: (
            category_rank.get(str(x.get("category", "")).lower(), 99),
            get_onboarding_step_rank(x.get("test_case", {})),
            x.get("endpoint", ""),
            priority_weight.get(x.get("priority"), 99),
            x.get("test_case", {}).get("name", ""),
            x.get("test_case_id", "")
        )
    )

    # Attach full per-API executions to report_dict (Item 7)
    report_dict["test_case_executions"] = test_case_executions
    report_dict["generation_errors"] = state.get("generation_errors", [])

    # 6. Render HTML and PDF (with fallback handling)
    html_content = ""
    pdf_content = b""

    try:
        html_content = ReportRendererTool.render_html(report_dict)
        if html_content:
            os.makedirs("reports", exist_ok=True)
            report_file_path = os.path.join("reports", f"report_{run_id}.html")
            with open(report_file_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            latest_file_path = os.path.join("reports", "latest_report.html")
            with open(latest_file_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info(f"[REPORTING_AGENT] Saved HTML visual report to '{report_file_path}' and '{latest_file_path}'.")
    except Exception as e:
        logger.warning(f"[REPORTING_AGENT] Report HTML rendering failed: {e}. Falling back to empty HTML.")

    try:
        if html_content:
            pdf_bytes = ReportRendererTool.render_pdf(report_dict)
            pdf_content = pdf_bytes
    except Exception as e:
        logger.warning(f"[REPORTING_AGENT] Report PDF rendering failed: {e}. Falling back to empty PDF.")

    # 7. Persist Report to Reports Collection
    try:
        report_record = {
            "_id": run_id,
            "run_id": run_id,
            "report_data": report_dict,
            "html_content": html_content,
            "pdf_content": Binary(pdf_content) if pdf_content else None,
            "created_at": current_utc_iso()
        }
        await db["reports"].replace_one({"_id": run_id}, report_record, upsert=True)
    except Exception as e:
        logger.error(f"[REPORTING_AGENT] Failed to persist report record: {e}")

    # Update run's final_report and test_case_executions in MongoDB in a single atomic update
    try:
        await db["runs"].update_one(
            {"_id": run_id},
            {
                "$set": {
                    "test_case_executions": test_case_executions,
                    "final_report": report_dict,
                    "status": "completed",
                    "updated_at": current_utc_iso()
                }
            }
        )
    except Exception as e:
        logger.error(f"[REPORTING_AGENT] Failed to update test_case_executions in runs: {e}")

    return {
        "final_report": report_dict,
        "status": "completed"
    }
