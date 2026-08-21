import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.graph.state import TestCase, TestRunState
from app.llm import get_llm_client
from app.llm.client import LLMClient
from app.llm.structured_output import StructuredOutputFailure, call_with_validation
from app.models.test_case import TestCaseGeneratedModel, TestCaseLLMInputListModel
from app.middleware.logging_middleware import ctx_run_id
from app.utils.ids import generate_test_case_id

logger = logging.getLogger("generator_base")

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts" / "generators"


async def generate_category_test_cases(
    category: str,
    prompt_filename: str,
    state_or_payload: Dict[str, Any],
    llm_client: Optional[LLMClient] = None
) -> Dict[str, Any]:
    """Base helper for LLM-based test case generation nodes."""
    # Propagate run_id into the contextvar so all log lines inside this
    # Send()-spawned task carry the correct run_id (not inherited from parent).
    run_id = state_or_payload.get("run_id", "")
    if run_id:
        ctx_run_id.set(run_id)

    endpoints = state_or_payload.get("endpoints", [])
    test_plan = state_or_payload.get("test_plan", {}).get("plan", {})

    # Filter endpoints targeting this category
    target_endpoints = []
    for ep in endpoints:
        op_id = ep.get("operation_id")
        op_plan = test_plan.get(op_id, {})
        if category in op_plan.get("categories", []):
            target_endpoints.append({
                "endpoint": ep,
                "target_count": op_plan.get("target_case_count", {}).get(category, 1),
                "priority": op_plan.get("priority", "medium")
            })

    if not target_endpoints:
        logger.info(f"[{category.upper()}_GENERATOR] No endpoints targeted for '{category}'. Skipping.")
        return {"test_cases": []}

    logger.info(f"[{category.upper()}_GENERATOR] Target endpoints for '{category}' ({len(target_endpoints)} total): {[item.get('endpoint', {}).get('operation_id') for item in target_endpoints]}")

    client = llm_client or get_llm_client()

    prompt_path = PROMPTS_DIR / prompt_filename
    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else f"Generate {category} API test cases."

    user_prompt = (
        f"Generate {category} test cases for the following target endpoints:\n\n"
        f"{json.dumps(target_endpoints, indent=2)}"
    )

    try:
        logger.info(f"[{category.upper()}_GENERATOR] Invoking LLM at temperature=0.4 for {len(target_endpoints)} endpoints...")
        result_model: TestCaseLLMInputListModel = await call_with_validation(
            client=client,
            prompt=user_prompt,
            response_model=TestCaseLLMInputListModel,
            system_prompt=system_prompt,
            temperature=0.4,
            max_retries=1
        )

        test_cases_list: List[Dict[str, Any]] = []
        for item in result_model.test_cases:
            tc_dict = item.model_dump()
            # Assign globally unique, category-prefixed test_case_id in code
            tc_dict["id"] = generate_test_case_id(category)
            tc_dict["category"] = category
            tc_dict["generated_by"] = f"{category}_generator_v1"

            # Strict validation against full TestCaseGeneratedModel
            full_model = TestCaseGeneratedModel(**tc_dict)
            test_cases_list.append(full_model.model_dump())

        logger.info(f"[{category.upper()}_GENERATOR] Successfully generated {len(test_cases_list)} test cases with unique IDs.")

        return {"test_cases": test_cases_list}

    except (StructuredOutputFailure, Exception) as e:
        logger.warning(
            f"[{category.upper()}_GENERATOR] LLM generation failed ({e}). "
            f"Generating fallback baseline test cases for {len(target_endpoints)} target endpoints."
        )
        fallback_list: List[Dict[str, Any]] = []
        for item in target_endpoints:
            ep = item.get("endpoint", {})
            op_id = ep.get("operation_id") or "endpoint_op"
            path = ep.get("path", "/")
            method = ep.get("method", "GET").upper()

            # Clean path template placeholders: e.g. {task_id} -> task_001, {id} -> 1, {username} -> user1
            clean_path = (
                path.replace("{task_id}", "task_001")
                    .replace("{id}", "1")
                    .replace("{petId}", "1")
                    .replace("{username}", "user1")
                    .replace("{orderId}", "1")
            )

            # Determine schema-valid body and expected status code
            body = None
            expected_code = 200

            if category == "functional":
                if method in ("POST", "PUT", "PATCH"):
                    if "user" in path or "user" in op_id:
                        body = {
                            "id": 1,
                            "username": "user1",
                            "firstName": "John",
                            "lastName": "Doe",
                            "email": "john@example.com",
                            "password": "password123",
                            "phone": "1234567890",
                            "userStatus": 1
                        }
                        if "array" in op_id.lower() or "list" in op_id.lower():
                            body = [body]
                    elif "order" in path or "order" in op_id:
                        body = {
                            "id": 1,
                            "petId": 1,
                            "quantity": 1,
                            "shipDate": "2026-08-05T10:00:00.000Z",
                            "status": "placed",
                            "complete": True
                        }
                    elif "pet" in path or "pet" in op_id:
                        body = {
                            "id": 1,
                            "name": "doggie",
                            "category": {"id": 1, "name": "Dogs"},
                            "photoUrls": ["http://example.com/photo.jpg"],
                            "tags": [{"id": 1, "name": "tag1"}],
                            "status": "available"
                        }
                    elif "task" in path or "task" in op_id:
                        body = {"title": "Sample Test Task", "description": "Automated test task", "priority": "high", "completed": False}
                    else:
                        body = {"title": "Test Item", "name": "Test Item"}
                if method == "POST":
                    expected_code = 200 if ("pet" in path or "user" in path or "order" in path) else 201
                elif method == "DELETE":
                    expected_code = 204 if ("204" in ep.get("response_schemas", {}) or "task" in path) else 200

            elif category == "negative":
                expected_code = 404 if "{" in path else 400
                if method == "POST":
                    body = {}  # missing required fields triggers 400 validation error
                elif "{" in path:
                    clean_path = (
                        path.replace("{task_id}", "nonexistent_id_9999")
                            .replace("{id}", "999999")
                            .replace("{petId}", "999999")
                            .replace("{username}", "nonexistent_user_9999")
                            .replace("{orderId}", "999999")
                    )

            elif category == "security":
                expected_code = 200
                if method in ("POST", "PUT", "PATCH"):
                    body = {"title": "Security Audit Task", "priority": "medium", "completed": False}

            tc_dict = {
                "id": generate_test_case_id(category),
                "endpoint_operation_id": op_id,
                "category": category,
                "name": f"Fallback {category.capitalize()} Test - {method} {clean_path}",
                "description": f"Fallback baseline test case generated for {op_id}.",
                "request": {
                    "method": method,
                    "path": clean_path,
                    "headers": {},
                    "query_params": {},
                    "body": body
                },
                "expected": {
                    "status_code": expected_code,
                    "response_schema_match": True,
                    "max_response_time_ms": 5000
                },
                "priority": item.get("priority", "medium"),
                "generated_by": f"{category}_fallback_generator_v1",
                "rationale": f"Fallback baseline generated due to LLM limit: {str(e)[:100]}"
            }
            try:
                full_model = TestCaseGeneratedModel(**tc_dict)
                fallback_list.append(full_model.model_dump())
            except Exception as ex:
                logger.warning(f"Failed to validate fallback model: {ex}")

        return {"test_cases": fallback_list}
