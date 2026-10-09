import json
import logging
from typing import Any, Dict, List, Optional
from jinja2 import Template
from fpdf import FPDF

logger = logging.getLogger("report_renderer")

DEFAULT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>API Test Execution Report - {{ run_id }}</title>
    <style>
        :root {
            --primary: #2563eb;
            --primary-dark: #1d4ed8;
            --bg: #f8fafc;
            --card-bg: #ffffff;
            --text: #0f172a;
            --text-muted: #64748b;
            --border: #e2e8f0;
            --pass: #10b981;
            --pass-bg: #d1fae5;
            --fail: #ef4444;
            --fail-bg: #fee2e2;
            --flaky: #f59e0b;
            --flaky-bg: #fef3c7;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: var(--bg); color: var(--text); padding: 32px 24px; line-height: 1.5; }
        .container { max-width: 1200px; margin: 0 auto; }
        
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 2px solid var(--border); }
        .header h1 { font-size: 1.75rem; font-weight: 700; color: #1e293b; }
        .header .timestamp { font-size: 0.875rem; color: var(--text-muted); }

        .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .meta-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        .meta-label { font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); margin-bottom: 6px; }
        .meta-value { font-size: 1.5rem; font-weight: 700; color: #0f172a; }

        .summary-box { background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%); border-left: 4px solid var(--primary); padding: 18px 20px; border-radius: 8px; margin-bottom: 32px; font-size: 0.95rem; color: #1e3a8a; }

        .section-title { font-size: 1.25rem; font-weight: 600; color: #1e293b; margin: 32px 0 14px 0; }

        .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 24px; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.875rem; }
        th { background: #f8fafc; font-weight: 600; color: #475569; padding: 12px 16px; border-bottom: 1px solid var(--border); }
        td { padding: 12px 16px; border-bottom: 1px solid var(--border); vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        tr:hover { background-color: #f8fafc; }

        .badge { display: inline-block; padding: 3px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }
        .badge-pass { background: var(--pass-bg); color: #065f46; }
        .badge-fail { background: var(--fail-bg); color: #991b1b; }
        .badge-flaky { background: var(--flaky-bg); color: #92400e; }
        .badge-crit { background: #fecdd3; color: #9f1239; }
        .badge-high { background: #fed7aa; color: #9a3412; }
        .badge-med { background: #e0e7ff; color: #3730a3; }
        .badge-low { background: #f1f5f9; color: #475569; }

        .code-snippet { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 0.8rem; background: #f1f5f9; padding: 2px 6px; border-radius: 4px; color: #0f172a; }
        .fix-box { background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 8px 12px; margin-top: 6px; font-size: 0.85rem; color: #166534; }
        pre.code-block { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 0.775rem; background: #f8fafc; border: 1px solid var(--border); border-radius: 4px; padding: 6px 8px; max-height: 90px; overflow: auto; white-space: pre-wrap; word-break: break-all; }

        html { scroll-behavior: smooth; }
        .clickable-link {
            color: #2563eb;
            text-decoration: none;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .clickable-link:hover {
            color: #1d4ed8;
            background: #dbeafe !important;
            text-decoration: underline;
        }
        :target {
            animation: highlightTarget 2.5s ease-out forwards;
        }
        @keyframes highlightTarget {
            0% { background-color: #fef08a !important; }
            50% { background-color: #fef9c3 !important; }
            100% { background-color: transparent; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>API Test Execution Report</h1>
                <div class="timestamp">Generated: {{ created_at or 'Just now' }} | Run: {{ run_id }}</div>
            </div>
            <div style="display: flex; gap: 10px; align-items: center;">
                {% if status == 'awaiting_human' or status == 'pending_review' %}
                <button id="bulkApproveBtn" onclick="bulkApproveRun('{{ run_id }}')" style="background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: white; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 6px; box-shadow: 0 2px 4px rgba(16,185,129,0.2);">
                    ⚡ Approve All & Resume
                </button>
                {% endif %}
                <span class="badge badge-pass" style="font-size: 0.9rem; padding: 6px 14px;">{{ status }}</span>
            </div>
        </div>

        {% if status == 'awaiting_human' or status == 'pending_review' %}
        <div id="bulkApproveBanner" style="background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 8px; padding: 14px 18px; margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center;">
            <div>
                <strong style="color: #065f46;">⚠️ Run Awaiting Human Review:</strong>
                <span style="color: #047857; margin-left: 6px;">One or more high-severity findings require approval. You can approve all findings in 1-click to immediately resume test execution.</span>
            </div>
            <button onclick="bulkApproveRun('{{ run_id }}')" style="background: #059669; color: white; border: none; padding: 6px 14px; border-radius: 6px; font-weight: 600; cursor: pointer;">
                Approve All Now
            </button>
        </div>
        <script>
        async function bulkApproveRun(runId) {
            const btn = document.getElementById('bulkApproveBtn');
            if (btn) { btn.innerText = 'Approving & Resuming...'; btn.disabled = true; }
            try {
                const resp = await fetch('/api/v1/defects/runs/' + runId + '/approve-all', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({reviewer: 'lead_reviewer', notes: 'Bulk approved via Report 1-Click action'})
                });
                const res = await resp.json();
                alert('Success! Approved ' + (res.approved_count || 0) + ' findings. Graph resumed.');
                window.location.reload();
            } catch(e) {
                alert('Failed to bulk approve: ' + e);
                if (btn) { btn.innerText = '⚡ Approve All & Resume'; btn.disabled = false; }
            }
        }
        </script>
        {% endif %}

        <div class="meta-grid">
            <div class="meta-card">
                <div class="meta-label">Pass Rate</div>
                <div class="meta-value" style="color: {% if summary.pass_rate >= 0.75 %}#10b981{% elif summary.pass_rate >= 0.5 %}#f59e0b{% else %}#ef4444{% endif %};">
                    {{ "%.1f"|format(summary.pass_rate * 100) }}%
                </div>
            </div>
            <div class="meta-card">
                <div class="meta-label">Total Executions</div>
                <div class="meta-value">{{ summary.total_executions }}</div>
            </div>
            <div class="meta-card">
                <div class="meta-label">Passed</div>
                <div class="meta-value" style="color: #10b981;">{{ summary.pass_count }}</div>
            </div>
            <div class="meta-card">
                <div class="meta-label">Failed / Issues</div>
                <div class="meta-value" style="color: #ef4444;">{{ summary.fail_count }}</div>
            </div>
            <div class="meta-card">
                <div class="meta-label">Flaky Tests</div>
                <div class="meta-value" style="color: #f59e0b;">{{ summary.flaky_count }}</div>
            </div>
        </div>

        {% if executive_summary %}
        <div class="summary-box">
            <strong>Executive Summary:</strong> {{ executive_summary }}
        </div>
        {% endif %}

        <h2 class="section-title">Category Breakdown</h2>
        <div class="card">
            <table>
                <thead>
                    <tr>
                        <th>Category</th>
                        <th>Passed</th>
                        <th>Failed</th>
                        <th>Flaky</th>
                        <th>Category Pass Rate</th>
                    </tr>
                </thead>
                <tbody>
                    {% for cat, metrics in summary.categories.items() %}
                    {% set cat_total = metrics.pass_count + metrics.fail_count + metrics.flaky_count %}
                    <tr>
                        <td><strong>{{ cat|capitalize }}</strong></td>
                        <td><span class="badge badge-pass">{{ metrics.pass_count }}</span></td>
                        <td><span class="badge {% if metrics.fail_count > 0 %}badge-fail{% else %}badge-low{% endif %}">{{ metrics.fail_count }}</span></td>
                        <td><span class="badge {% if metrics.flaky_count > 0 %}badge-flaky{% else %}badge-low{% endif %}">{{ metrics.flaky_count }}</span></td>
                        <td>
                            {% if cat_total > 0 %}
                            {{ "%.1f"|format((metrics.pass_count / cat_total) * 100) }}%
                            {% else %}
                            N/A
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <!-- Section: Results by API Endpoint & Test Track -->
        <h2 class="section-title">Results by API Endpoint</h2>
        {% for track in track_sections %}
        <div style="margin-top: 24px; margin-bottom: 16px;">
            <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 4px;">
                <h3 style="font-size: 1.1rem; font-weight: 700; color: #1e293b; margin: 0;">{{ track.title }}</h3>
                <span class="badge {{ track.badge_class }}">{{ track.badge_text }}</span>
            </div>
            <p style="color: var(--text-muted); font-size: 0.85rem;">{{ track.subtitle }}</p>
        </div>

        {% for ep in track.endpoints %}
        <div class="card">
            <div style="padding: 14px 18px; background: #f8fafc; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                <div>
                    <span class="badge" style="background: #e0e7ff; color: #3730a3; margin-right: 8px;">{{ ep.method }}</span>
                    <strong style="font-size: 1rem; color: #0f172a;">{{ ep.path }}</strong>
                    <span class="code-snippet" style="margin-left: 8px;">{{ ep.operation_id }}</span>
                </div>
                <div style="display: flex; gap: 8px; align-items: center;">
                    <span class="badge badge-pass">Pass: {{ ep.pass_count }}</span>
                    <span class="badge {% if ep.fail_count > 0 %}badge-fail{% else %}badge-low{% endif %}">Fail: {{ ep.fail_count }}</span>
                    {% if ep.flaky_count > 0 %}<span class="badge badge-flaky">Flaky: {{ ep.flaky_count }}</span>{% endif %}
                    {% if ep.not_executed_count > 0 %}<span class="badge badge-low">Not Executed: {{ ep.not_executed_count }}</span>{% endif %}
                    <span style="font-weight: 700; margin-left: 6px; color: {% if ep.pass_rate >= 0.75 %}#10b981{% elif ep.pass_rate >= 0.5 %}#f59e0b{% else %}#ef4444{% endif %};">{{ "%.1f"|format(ep.pass_rate * 100) }}%</span>
                </div>
            </div>
            <table>
                <thead>
                    <tr>
                        <th style="width: 100px;">Category</th>
                        <th style="width: 160px;">Test Name</th>
                        <th style="width: 220px;">Request Payload</th>
                        <th style="width: 75px;">Expected</th>
                        <th style="width: 75px;">Actual</th>
                        <th style="width: 90px;">Status</th>
                        <th>Response / Assertion Failure</th>
                    </tr>
                </thead>
                <tbody>
                    {% for tc in ep.test_cases %}
                    <tr id="tc-{{ tc.test_case_id }}">
                        <td><span class="badge badge-med">{{ tc.category }}</span></td>
                        <td>
                            <strong>{{ tc.test_case.name or tc.test_case_id }}</strong>
                            <div style="margin-top: 4px;">
                                <span class="code-snippet" style="font-size: 0.72rem; color: #64748b;">{{ tc.test_case_id }}</span>
                            </div>
                        </td>
                        <td>
                            {% if tc.test_case.request.body %}
                            <pre class="code-block">{{ tc.test_case.request.body|tojson(indent=2) }}</pre>
                            {% else %}
                            <span style="color: #94a3b8;">None</span>
                            {% endif %}
                        </td>
                        <td><span class="code-snippet">{{ tc.test_case.expected.status_code or '-' }}</span></td>
                        <td><span class="code-snippet">{{ tc.execution_result.actual_status_code or '-' }}</span></td>
                        <td>
                            <span class="badge {% if tc.execution_status == 'PASS' %}badge-pass{% elif tc.execution_status == 'FLAKY' %}badge-flaky{% elif tc.execution_status == 'NOT_EXECUTED' %}badge-low{% else %}badge-fail{% endif %}">
                                {{ tc.execution_status }}
                            </span>
                        </td>
                        <td>
                            {% if tc.execution_result.actual_response %}
                            <pre class="code-block">{{ (tc.execution_result.actual_response|tojson)|truncate(180) }}</pre>
                            {% elif tc.execution_result.assertion_failures %}
                            <div style="color: #b91c1c; font-size: 0.8rem;">{{ tc.execution_result.assertion_failures[0] }}</div>
                            {% else %}
                            <span style="color: #94a3b8;">-</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                    {% if not ep.test_cases %}
                    <tr>
                        <td colspan="7" style="text-align: center; color: #64748b; padding: 16px;">
                            No test executions recorded for this endpoint.
                        </td>
                    </tr>
                    {% endif %}
                </tbody>
            </table>
        </div>
        {% endfor %}
        {% endfor %}

        <h2 class="section-title">Findings, Diagnostics &amp; Suggested Fixes</h2>
        <div class="card">
            <table>
                <thead>
                    <tr>
                        <th style="width: 130px;">Finding ID</th>
                        <th style="width: 150px;">Test Case</th>
                        <th style="width: 100px;">Severity</th>
                        <th style="width: 90px;">Confidence</th>
                        <th>Root Cause &amp; Suggested Fix</th>
                    </tr>
                </thead>
                <tbody>
                    {% for f in findings %}
                    <tr id="finding-{{ f.id }}">
                        <td>
                            <a href="#tc-{{ f.test_case_id }}" class="clickable-link code-snippet" title="Jump to exact test case execution result above">
                                {{ f.id }} ↗
                            </a>
                        </td>
                        <td>
                            <a href="#tc-{{ f.test_case_id }}" class="clickable-link code-snippet" title="Jump to exact test case execution result above">
                                {{ f.test_case_id }} ↗
                            </a>
                        </td>
                        <td>
                            <span class="badge {% if f.severity == 'critical' %}badge-crit{% elif f.severity == 'high' %}badge-high{% elif f.severity == 'medium' %}badge-med{% else %}badge-low{% endif %}">
                                {{ f.severity }}
                            </span>
                        </td>
                        <td>{{ "%.2f"|format(f.confidence) }}</td>
                        <td>
                            <div><strong>{{ f.root_cause_summary or 'Unspecified failure reason.' }}</strong></div>
                            {% if f.suggested_fix %}
                            <div class="fix-box">
                                <strong>Suggested Fix:</strong> {{ f.suggested_fix }}
                            </div>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                    {% if not findings %}
                    <tr>
                        <td colspan="5" style="text-align: center; color: #64748b; padding: 24px;">
                            🎉 No defects or findings identified. All target expectations met!
                        </td>
                    </tr>
                    {% endif %}
                </tbody>
            </table>
        </div>

        {% if generation_errors %}
        <h2 class="section-title" style="color: #d97706;">Generation Warnings &amp; LLM Fallbacks</h2>
        <div class="card">
            <table>
                <thead>
                    <tr>
                        <th style="width: 30%;">Operation ID</th>
                        <th style="width: 14%;">Category</th>
                        <th style="width: 22%;">Fallback Strategy</th>
                        <th>Summary / Reason</th>
                    </tr>
                </thead>
                <tbody>
                    {% for err in generation_errors %}
                    <tr>
                        <td><span class="code-snippet">{{ err.operation_id }}</span></td>
                        <td><span class="badge badge-med">{{ err.category }}</span></td>
                        <td><span class="badge" style="background: #fef3c7; color: #92400e;">{{ err.fallback }}</span></td>
                        <td style="color: #b45309; font-size: 0.85rem; word-break: break-word;">{{ err.error }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""


def _sanitize_gen_error_text(err: Any) -> str:
    """Extract a concise, human-readable summary from raw LLM error messages/exceptions."""
    from app.config.settings import settings
    provider_name = (settings.LLM_PROVIDER or "LLM").capitalize()
    err_str = str(err)
    if "402" in err_str or "requires more credits" in err_str.lower() or "insufficient" in err_str.lower():
        return f"Insufficient LLM Credits (HTTP 402): {provider_name} quota exceeded. Deterministic schema fallback used."
    if "429" in err_str or "rate limit" in err_str.lower():
        return f"Rate Limit Exceeded (HTTP 429): {provider_name} request throttled. Deterministic schema fallback used."
    if "401" in err_str or "invalid api key" in err_str.lower() or "unauthorized" in err_str.lower():
        return f"Authentication Error (HTTP 401): Invalid {provider_name} API key. Deterministic schema fallback used."
    if "timeout" in err_str.lower() or "timed out" in err_str.lower():
        return f"Timeout: {provider_name} request timed out. Deterministic schema fallback used."
    if "parse" in err_str.lower() or "json" in err_str.lower() or "validation" in err_str.lower():
        return "Malformed Response: LLM output structure invalid. Deterministic schema fallback used."
    first_line = err_str.split("\n")[0].strip()
    if len(first_line) > 130:
        first_line = first_line[:127] + "..."
    return first_line


class PDFReport(FPDF):
    """FPDF subclass defining simple header and footer layouts."""
    def header(self):
        self.set_font('helvetica', 'B', 15)
        self.cell(0, 10, 'API Test Execution Report', border=False, align='C', new_x="LMARGIN", new_y="NEXT")
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font('helvetica', 'I', 8)
        self.cell(0, 10, f'Page {self.page_no()}/{{nb}}', align='C')


def _clean_latin1(text: Any) -> str:
    """Sanitize unicode characters to latin-1 compatible ASCII/dash characters for FPDF."""
    if not text:
        return ""
    t = str(text)
    replacements = {
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2022": "*",
        "\u2026": "...",
    }
    for k, v in replacements.items():
        t = t.replace(k, v)
    return t.encode("latin-1", "replace").decode("latin-1")


class ReportRendererTool:
    """Tool for rendering the final JSON report document into HTML and PDF."""

    @staticmethod
    def render_html(report_dict: Dict[str, Any]) -> str:
        """Render report dictionary into HTML format using a Jinja2 template."""
        try:
            tc_execs = report_dict.get("test_case_executions", [])

            # Helper to group executions by endpoint within a track while maintaining onboarding order
            def _group_by_endpoint(exec_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                grouped: Dict[str, Dict[str, Any]] = {}
                for item in exec_list:
                    op_id = item.get("endpoint_operation_id") or "unknown_operation"
                    if op_id not in grouped:
                        grouped[op_id] = {
                            "operation_id": op_id,
                            "method": item.get("http_method") or item.get("method") or "GET",
                            "path": item.get("endpoint_path") or item.get("path") or "/",
                            "pass_count": 0,
                            "fail_count": 0,
                            "flaky_count": 0,
                            "not_executed_count": 0,
                            "test_cases": []
                        }
                    stat = str(item.get("execution_status", "")).upper()
                    if stat == "PASS":
                        grouped[op_id]["pass_count"] += 1
                    elif stat == "FLAKY":
                        grouped[op_id]["flaky_count"] += 1
                    elif stat == "NOT_EXECUTED":
                        grouped[op_id]["not_executed_count"] += 1
                    else:
                        grouped[op_id]["fail_count"] += 1
                    grouped[op_id]["test_cases"].append(item)

                for ep_data in grouped.values():
                    tot = ep_data["pass_count"] + ep_data["fail_count"] + ep_data["flaky_count"] + ep_data["not_executed_count"]
                    ep_data["pass_rate"] = (ep_data["pass_count"] / tot) if tot > 0 else 1.0
                return list(grouped.values())

            # 1. Functional / Positive Track
            func_execs = [item for item in tc_execs if str(item.get("category", "")).lower() == "functional"]
            # 2. Negative Track
            neg_execs = [item for item in tc_execs if str(item.get("category", "")).lower() == "negative"]
            # 3. Other Categories (edge_case, security, auth, authz, regression)
            other_execs = [item for item in tc_execs if str(item.get("category", "")).lower() not in ("functional", "negative")]

            track_sections = []
            if func_execs:
                track_sections.append({
                    "title": "Positive Tested APIs (Functional Flow)",
                    "subtitle": "Sequential happy-path workflow execution sharing unified live session state",
                    "badge_class": "badge-pass",
                    "badge_text": "Positive / Functional",
                    "endpoints": _group_by_endpoint(func_execs)
                })
            if neg_execs:
                track_sections.append({
                    "title": "Negative Tested APIs (Negative / Sandbox Validation)",
                    "subtitle": "Negative input validation and isolated boundary sandboxes with fresh session IDs",
                    "badge_class": "badge-fail",
                    "badge_text": "Negative / Boundary",
                    "endpoints": _group_by_endpoint(neg_execs)
                })
            if other_execs:
                track_sections.append({
                    "title": "Edge Case & Security Tested APIs",
                    "subtitle": "Security, authentication, authorization, and edge case suites",
                    "badge_class": "badge-med",
                    "badge_text": "Edge / Security",
                    "endpoints": _group_by_endpoint(other_execs)
                })

            # If no track_sections matched (e.g. empty or legacy), fallback to all grouped
            if not track_sections and tc_execs:
                track_sections.append({
                    "title": "API Test Executions",
                    "subtitle": "All recorded test case executions",
                    "badge_class": "badge-med",
                    "badge_text": "All Categories",
                    "endpoints": _group_by_endpoint(tc_execs)
                })

            # Sanitize generation errors for clean presentation
            sanitized_errors = []
            for err in report_dict.get("generation_errors", []):
                if isinstance(err, dict):
                    sanitized_errors.append({
                        "operation_id": err.get("operation_id") or "All Operations",
                        "category": str(err.get("category") or "Generation").upper(),
                        "fallback": err.get("fallback") or "Deterministic Schema Generator",
                        "error": _sanitize_gen_error_text(err.get("error", ""))
                    })
                else:
                    sanitized_errors.append({
                        "operation_id": "All Operations",
                        "category": "GENERATION",
                        "fallback": "Deterministic Schema Generator",
                        "error": _sanitize_gen_error_text(str(err))
                    })

            render_context = dict(report_dict)
            render_context["track_sections"] = track_sections
            render_context["generation_errors"] = sanitized_errors

            template = Template(DEFAULT_HTML_TEMPLATE)
            return template.render(**render_context)
        except Exception as e:
            logger.error(f"HTML Report rendering failed: {e}")
            raise

    @staticmethod
    def render_pdf(report_dict: Dict[str, Any]) -> bytes:
        """Render report dictionary into raw PDF bytes using FPDF."""
        try:
            pdf = PDFReport()
            pdf.add_page()
            pdf.set_font("helvetica", size=10)

            # Metadata Section
            pdf.set_font("helvetica", "B", 11)
            pdf.cell(40, 8, "Run ID:")
            pdf.set_font("helvetica", "", 11)
            pdf.cell(0, 8, str(report_dict.get("run_id")), new_x="LMARGIN", new_y="NEXT")

            pdf.set_font("helvetica", "B", 11)
            pdf.cell(40, 8, "Status:")
            pdf.set_font("helvetica", "", 11)
            pdf.cell(0, 8, str(report_dict.get("status")), new_x="LMARGIN", new_y="NEXT")

            pdf.set_font("helvetica", "B", 11)
            pdf.cell(40, 8, "Pass Rate:")
            pdf.set_font("helvetica", "", 11)
            summary = report_dict.get("summary", {})
            pass_rate = summary.get("pass_rate", 0.0)
            pdf.cell(0, 8, f"{pass_rate * 100:.2f}%", new_x="LMARGIN", new_y="NEXT")

            pdf.set_font("helvetica", "B", 11)
            pdf.cell(40, 8, "Executions:")
            pdf.set_font("helvetica", "", 11)
            exec_str = (
                f"Total: {summary.get('total_executions')} "
                f"(Pass: {summary.get('pass_count')}, "
                f"Fail: {summary.get('fail_count')}, "
                f"Flaky: {summary.get('flaky_count')})"
            )
            pdf.cell(0, 8, exec_str, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(5)

            # Executive Summary Section
            exec_summary = report_dict.get("executive_summary")
            if exec_summary:
                pdf.set_font("helvetica", "B", 12)
                pdf.cell(0, 10, "Executive Summary", new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("helvetica", "I", 10)
                pdf.multi_cell(0, 5, _clean_latin1(exec_summary))
                pdf.ln(5)

            # Categories Table
            pdf.set_font("helvetica", "B", 12)
            pdf.cell(0, 10, "Category Breakdown", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("helvetica", "B", 10)
            pdf.cell(60, 8, "Category", border=1)
            pdf.cell(40, 8, "Pass Count", border=1)
            pdf.cell(40, 8, "Fail Count", border=1)
            pdf.cell(40, 8, "Flaky Count", border=1, new_x="LMARGIN", new_y="NEXT")

            pdf.set_font("helvetica", "", 10)
            categories = summary.get("categories", {})
            for cat, m in categories.items():
                pdf.cell(60, 8, _clean_latin1(str(cat)), border=1)
                pdf.cell(40, 8, str(m.get("pass_count", 0)), border=1)
                pdf.cell(40, 8, str(m.get("fail_count", 0)), border=1)
                pdf.cell(40, 8, str(m.get("flaky_count", 0)), border=1, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(5)

            # Endpoints Breakdown Section
            endpoints = summary.get("endpoints", {})
            if endpoints:
                pdf.set_font("helvetica", "B", 12)
                pdf.cell(0, 10, "Results by API Endpoint", new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("helvetica", "B", 9)
                pdf.cell(60, 8, "Operation / Endpoint", border=1)
                pdf.cell(25, 8, "Method", border=1)
                pdf.cell(25, 8, "Passed", border=1)
                pdf.cell(25, 8, "Failed", border=1)
                pdf.cell(25, 8, "Pass Rate", border=1, new_x="LMARGIN", new_y="NEXT")

                pdf.set_font("helvetica", "", 9)
                for op_id, ep_info in endpoints.items():
                    pdf.cell(60, 8, _clean_latin1(str(op_id)[:25]), border=1)
                    pdf.cell(25, 8, _clean_latin1(str(ep_info.get("method", "GET"))), border=1)
                    pdf.cell(25, 8, str(ep_info.get("pass_count", 0)), border=1)
                    pdf.cell(25, 8, str(ep_info.get("fail_count", 0)), border=1)
                    pdf.cell(25, 8, f"{ep_info.get('pass_rate', 0.0) * 100:.1f}%", border=1, new_x="LMARGIN", new_y="NEXT")
                pdf.ln(5)

            # Findings Section
            pdf.set_font("helvetica", "B", 12)
            pdf.cell(0, 10, "Identified Findings & Defects", new_x="LMARGIN", new_y="NEXT")
            findings = report_dict.get("findings", [])
            if not findings:
                pdf.set_font("helvetica", "I", 10)
                pdf.cell(0, 8, "No findings or defects identified in this run.", new_x="LMARGIN", new_y="NEXT")
            else:
                for f in findings:
                    pdf.set_font("helvetica", "B", 10)
                    pdf.cell(
                        0, 8,
                        _clean_latin1(f"{f.get('id')} - Severity: {f.get('severity')} (Confidence: {f.get('confidence', 0.0):.2f})"),
                        new_x="LMARGIN", new_y="NEXT"
                    )
                    pdf.set_font("helvetica", "", 10)
                    summary_text = f.get("root_cause_summary", "No details provided.")
                    fix_text = f.get("suggested_fix", "No fix provided.")
                    pdf.multi_cell(0, 5, _clean_latin1(f"Summary: {summary_text}\nSuggested Fix: {fix_text}"))
                    pdf.ln(2)

            # Generation Warnings Section
            gen_errors = report_dict.get("generation_errors", [])
            if gen_errors:
                pdf.ln(4)
                pdf.set_font("helvetica", "B", 12)
                pdf.cell(0, 10, "Generation Warnings & LLM Fallbacks", new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("helvetica", "", 9)
                for err in gen_errors:
                    if isinstance(err, dict):
                        op = str(err.get("operation_id", "All"))[:35]
                        cat = str(err.get("category", "General")).upper()
                        clean_reason = _sanitize_gen_error_text(err.get("error", ""))
                        pdf.multi_cell(0, 5, _clean_latin1(f"- [{op}] ({cat}): {clean_reason}"))
                    else:
                        pdf.multi_cell(0, 5, _clean_latin1(f"- {_sanitize_gen_error_text(str(err))}"))
                    pdf.ln(1)

            return bytes(pdf.output())
        except Exception as e:
            logger.error(f"PDF Report rendering failed: {e}")
            raise
