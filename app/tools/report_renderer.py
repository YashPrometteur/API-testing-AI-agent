import logging
from typing import Any, Dict, Optional
from jinja2 import Template
from fpdf import FPDF

logger = logging.getLogger("report_renderer")

DEFAULT_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>API Test Execution Report</title>
    <style>
        body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; color: #333; margin: 40px; line-height: 1.6; }
        h1 { border-bottom: 2px solid #3b82f6; padding-bottom: 10px; color: #1e3a8a; }
        h2 { color: #1e40af; margin-top: 30px; border-bottom: 1px solid #e5e7eb; padding-bottom: 5px; }
        .meta-box { background-color: #f3f4f6; border-radius: 8px; padding: 20px; margin-bottom: 20px; display: flex; flex-wrap: wrap; gap: 20px; }
        .meta-item { flex: 1 1 200px; }
        .meta-label { font-weight: bold; color: #4b5563; font-size: 0.9em; }
        .meta-val { font-size: 1.1em; color: #111827; }
        .summary-paragraph { background-color: #eff6ff; border-left: 4px solid #3b82f6; padding: 15px; border-radius: 0 8px 8px 0; margin-bottom: 30px; font-style: italic; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; margin-bottom: 30px; }
        th, td { text-align: left; padding: 12px; border-bottom: 1px solid #e5e7eb; }
        th { background-color: #f9fafb; font-weight: bold; color: #374151; }
        tr:hover { background-color: #f9fafb; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; font-weight: bold; }
        .badge-pass { background-color: #d1fae5; color: #065f46; }
        .badge-fail { background-color: #fee2e2; color: #991b1b; }
        .badge-flaky { background-color: #fef3c7; color: #92400e; }
        .badge-crit { background-color: #fca5a5; color: #7f1d1d; }
        .badge-high { background-color: #fed7aa; color: #7c2d12; }
    </style>
</head>
<body>
    <h1>API Test Execution Report</h1>
    
    <div class="meta-box">
        <div class="meta-item">
            <div class="meta-label">Run ID</div>
            <div class="meta-val">{{ run_id }}</div>
        </div>
        <div class="meta-item">
            <div class="meta-label">Status</div>
            <div class="meta-val"><span class="badge badge-pass">{{ status }}</span></div>
        </div>
        <div class="meta-item">
            <div class="meta-label">Pass Rate</div>
            <div class="meta-val">{{ "%.2f"|format(summary.pass_rate * 100) }}%</div>
        </div>
        <div class="meta-item">
            <div class="meta-label">Total Executions</div>
            <div class="meta-val">{{ summary.total_executions }}</div>
        </div>
    </div>

    {% if executive_summary %}
    <h2>Executive Summary</h2>
    <div class="summary-paragraph">
        {{ executive_summary }}
    </div>
    {% endif %}

    <h2>Execution Summary Metrics</h2>
    <table>
        <thead>
            <tr>
                <th>Status</th>
                <th>Count</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td>Passed</td>
                <td><span class="badge badge-pass">{{ summary.pass_count }}</span></td>
            </tr>
            <tr>
                <td>Failed / Errors</td>
                <td><span class="badge badge-fail">{{ summary.fail_count }}</span></td>
            </tr>
            <tr>
                <td>Flaky</td>
                <td><span class="badge badge-flaky">{{ summary.flaky_count }}</span></td>
            </tr>
        </tbody>
    </table>

    <h2>Category Summary</h2>
    <table>
        <thead>
            <tr>
                <th>Category</th>
                <th>Passed</th>
                <th>Failed</th>
                <th>Flaky</th>
            </tr>
        </thead>
        <tbody>
            {% for cat, metrics in summary.categories.items() %}
            <tr>
                <td>{{ cat }}</td>
                <td>{{ metrics.pass_count }}</td>
                <td>{{ metrics.fail_count }}</td>
                <td>{{ metrics.flaky_count }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    <h2>Findings &amp; Defects</h2>
    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Test Case ID</th>
                <th>Severity</th>
                <th>Confidence</th>
                <th>Status</th>
                <th>Description</th>
            </tr>
        </thead>
        <tbody>
            {% for f in findings %}
            <tr>
                <td>{{ f.id }}</td>
                <td>{{ f.test_case_id }}</td>
                <td><span class="badge {% if f.severity in ('critical', 'high') %}badge-crit{% else %}badge-flaky{% endif %}">{{ f.severity }}</span></td>
                <td>{{ "%.2f"|format(f.confidence) }}</td>
                <td>{{ f.guardrail_status }}</td>
                <td>{{ f.root_cause_summary }}</td>
            </tr>
            {% endfor %}
            {% if not findings %}
            <tr>
                <td colspan="6">No findings or defects identified in this run.</td>
            </tr>
            {% endif %}
        </tbody>
    </table>
</body>
</html>
"""


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


class ReportRendererTool:
    """Tool for rendering the final JSON report document into HTML and PDF."""

    @staticmethod
    def render_html(report_dict: Dict[str, Any]) -> str:
        """Render report dictionary into HTML format using a Jinja2 template."""
        try:
            template = Template(DEFAULT_HTML_TEMPLATE)
            return template.render(**report_dict)
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
                pdf.multi_cell(0, 5, exec_summary)
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
                pdf.cell(60, 8, str(cat), border=1)
                pdf.cell(40, 8, str(m.get("pass_count", 0)), border=1)
                pdf.cell(40, 8, str(m.get("fail_count", 0)), border=1)
                pdf.cell(40, 8, str(m.get("flaky_count", 0)), border=1, new_x="LMARGIN", new_y="NEXT")
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
                        f"{f.get('id')} - Severity: {f.get('severity')} "
                        f"(Confidence: {f.get('confidence', 0.0):.2f})",
                        new_x="LMARGIN", new_y="NEXT"
                    )
                    pdf.set_font("helvetica", "", 10)
                    summary_text = f.get("root_cause_summary", "No details provided.")
                    fix_text = f.get("suggested_fix", "No fix provided.")
                    pdf.multi_cell(0, 5, f"Summary: {summary_text}\nSuggested Fix: {fix_text}")
                    pdf.ln(2)

            return bytes(pdf.output())
        except Exception as e:
            logger.error(f"PDF Report rendering failed: {e}")
            raise
