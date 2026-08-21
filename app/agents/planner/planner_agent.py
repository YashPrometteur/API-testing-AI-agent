import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from app.graph.state import TestRunState
from app.llm import get_llm_client
from app.llm.client import LLMClient
from app.llm.structured_output import StructuredOutputFailure, call_with_validation
from app.models.test_plan import TestPlan

logger = logging.getLogger("planner_agent")

PROMPT_FILE = Path(__file__).parent.parent.parent / "prompts" / "planner" / "planner_v1.md"


class PlannerAgent:
    """LLM-powered Planner Agent. Analyzes EndpointSpec list and generates a TestPlan model."""

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm_client = llm_client

    async def __call__(self, state: TestRunState) -> Dict[str, Any]:
        endpoints = state.get("endpoints")
        if not endpoints:
            return {
                "status": "failed",
                "errors": ["PlannerAgent received empty endpoints list"]
            }

        client = self.llm_client or get_llm_client()

        # Load system prompt template
        system_prompt = ""
        if PROMPT_FILE.exists():
            system_prompt = PROMPT_FILE.read_text(encoding="utf-8")
        else:
            system_prompt = (
                "You are an API Test Planning Agent. Analyze EndpointSpec list and generate "
                "a comprehensive TestPlan mapping operation_id to test categories, case counts, and priority."
            )

        prompt_user = (
            f"Analyze the following OpenAPI endpoints and generate a TestPlan:\n\n"
            f"{json.dumps(endpoints, indent=2)}"
        )

        requested_categories = state.get("categories") or []
        target_cats = [c.lower() for c in requested_categories] if requested_categories else ["functional", "negative", "security"]

        try:
            logger.info(f"PlannerAgent received {len(endpoints)} endpoints: {[ep.get('operation_id') for ep in endpoints if isinstance(ep, dict)]}")
            logger.info("PlannerAgent invoking LLM for test plan generation...")
            test_plan_model: TestPlan = await call_with_validation(
                client=client,
                prompt=prompt_user,
                response_model=TestPlan,
                system_prompt=system_prompt,
                temperature=0.2,
                max_retries=1
            )

            test_plan_dict = test_plan_model.model_dump()
            if target_cats:
                plan_map = test_plan_dict.get("plan", {})
                for op_id, op_plan in plan_map.items():
                    existing_counts = op_plan.get("target_case_count", {})
                    op_plan["categories"] = list(target_cats)
                    op_plan["target_case_count"] = {
                        cat: existing_counts.get(cat, 1) for cat in target_cats
                    }
                logger.info(f"PlannerAgent enforced user-requested categories {target_cats} across {len(plan_map)} operations (overriding any LLM-proposed subset).")

            logger.info(f"PlannerAgent generated test plan for {len(test_plan_dict.get('plan', {}))} operations: {list(test_plan_dict.get('plan', {}).keys())}")

            return {
                "test_plan": test_plan_dict,
                "status": "running"
            }

        except (StructuredOutputFailure, Exception) as e:
            logger.warning(
                f"PlannerAgent LLM call failed ({e}). "
                f"Constructing deterministic fallback test plan for {len(endpoints)} endpoints."
            )
            fallback_plan = {}
            for ep in endpoints:
                op_id = ep.get("operation_id") or f"op_{ep.get('method', 'get').lower()}_{ep.get('path', '').replace('/', '_')}"
                fallback_plan[op_id] = {
                    "categories": target_cats,
                    "target_case_count": {c: 1 for c in target_cats},
                    "priority": "medium"
                }
            return {
                "test_plan": {"plan": fallback_plan},
                "status": "running"
            }
