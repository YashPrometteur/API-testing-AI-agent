import asyncio
import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from app.graph.state import TestCase, TestRunState
from app.llm import get_llm_client
from app.llm.client import LLMClient
from app.llm.structured_output import StructuredOutputFailure, call_with_validation
from app.models.test_case import TestCaseGeneratedModel, TestCaseLLMInputListModel
from app.middleware.logging_middleware import ctx_run_id
from app.repositories.test_case_repository import TestCaseRepository
from app.tools.payload_generator import SchemaPayloadGenerator
from app.utils.ids import generate_test_case_id

logger = logging.getLogger("generator_base")

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts" / "generators"


def _normalize_test_case_key(tc: Dict[str, Any]) -> str:
    """Compute deduplication signature for test cases."""
    req = tc.get("request", {})
    method = req.get("method", "GET").upper()
    path = req.get("path", "")
    body_str = json.dumps(req.get("body"), sort_keys=True) if req.get("body") is not None else ""
    query_str = json.dumps(req.get("query_params"), sort_keys=True) if req.get("query_params") else ""
    return f"{method}:{path}:{query_str}:{body_str}"


def sanitize_generation_error_message(err: Any) -> str:
    """Transform raw LLM exceptions or strings into concise, human-readable fallback reasons."""
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
    if len(first_line) > 120:
        first_line = first_line[:117] + "..."
    return f"LLM generation failed: {first_line}. Deterministic schema fallback used."


async def generate_category_test_cases(
    category: str,
    prompt_filename: str,
    state_or_payload: Dict[str, Any],
    llm_client: Optional[LLMClient] = None
) -> Dict[str, Any]:
    """Base helper for test case generation: Combines deterministic schema generator with LLM."""
    run_id = state_or_payload.get("run_id", "")
    if run_id:
        ctx_run_id.set(run_id)

    spec_id = state_or_payload.get("spec_id", "")
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

    llm_generated_cases_by_op: Dict[str, List[Dict[str, Any]]] = {item.get("endpoint", {}).get("operation_id"): [] for item in target_endpoints}
    generation_errors: List[str] = []

    # Batch target endpoints into chunks of 5 to minimize LLM roundtrips and latency
    chunk_size = 5
    for chunk_idx in range(0, len(target_endpoints), chunk_size):
        chunk = target_endpoints[chunk_idx:chunk_idx + chunk_size]
        compact_chunk = []
        for target_item in chunk:
            ep_raw = target_item.get("endpoint", {})
            compact_chunk.append({
                "operation_id": ep_raw.get("operation_id"),
                "method": ep_raw.get("method"),
                "path": ep_raw.get("path"),
                "summary": ep_raw.get("summary"),
                "parameters": ep_raw.get("parameters", []),
                "request_schema": ep_raw.get("request_schema", {}),
                "target_count": target_item.get("target_count", 1),
                "priority": target_item.get("priority", "medium")
            })

        # Build explicit count directives per operation_id as requested by TL specification
        directives = []
        for target_item in chunk:
            op_id = target_item.get("endpoint", {}).get("operation_id") or "endpoint_op"
            count = target_item.get("target_count", 1)
            directives.append(f'- For operation_id "{op_id}", generate EXACTLY {count} {category} test cases.')
        count_instructions = "\n".join(directives)

        user_prompt = (
            f"Generate {category} test cases for the following target endpoints.\n\n"
            f"Explicit Requirements:\n"
            f"{count_instructions}\n"
            f"- Return empty list for operations not listed below.\n\n"
            f"Target Endpoint Definitions:\n"
            f"{json.dumps(compact_chunk, indent=2)}"
        )

        result_model = None
        last_chunk_err = None
        try:
            logger.info(f"[{category.upper()}_GENERATOR] Invoking LLM for chunk {chunk_idx // chunk_size + 1}...")
            result_model = await call_with_validation(
                client=client,
                prompt=user_prompt,
                response_model=TestCaseLLMInputListModel,
                system_prompt=system_prompt,
                temperature=0.4,
                max_retries=1
            )
        except Exception as chunk_exc:
            last_chunk_err = chunk_exc
            logger.warning(f"[{category.upper()}_GENERATOR] Chunk {chunk_idx // chunk_size + 1} failed: {chunk_exc}.")

        if result_model is not None:
            for item in result_model.test_cases:
                tc_dict = item.model_dump()
                tc_dict["id"] = generate_test_case_id(category)
                tc_dict["category"] = category
                tc_dict["generated_by"] = f"{category}_generator_v1"
                
                # Standardize max_latency_ms
                expected = tc_dict.get("expected", {})
                if "max_response_time_ms" in expected and "max_latency_ms" not in expected:
                    expected["max_latency_ms"] = expected.pop("max_response_time_ms")
                elif "max_latency_ms" not in expected or expected["max_latency_ms"] is None:
                    expected["max_latency_ms"] = 5000.0
                tc_dict["expected"] = expected

                op_id = tc_dict.get("endpoint_operation_id")
                if op_id in llm_generated_cases_by_op:
                    llm_generated_cases_by_op[op_id].append(tc_dict)
                else:
                    llm_generated_cases_by_op[op_id] = [tc_dict]
        else:
            e = last_chunk_err or RuntimeError("LLM chunk generation failed")
            clean_err = sanitize_generation_error_message(e)
            logger.warning(f"[{category.upper()}_GENERATOR] LLM generation issue for chunk: {clean_err}")
            logger.error(f"[{category.upper()}_GENERATOR] Raw Exception details: {e}")
            for item in chunk:
                ep = item.get("endpoint", {})
                op_id = ep.get("operation_id") or "endpoint_op"
                generation_errors.append({
                    "operation_id": op_id,
                    "category": category,
                    "error": clean_err,
                    "fallback": "deterministic_schema_generator"
                })
            continue

    # 2. Merge, deduplicate, and enforce target_case_count per endpoint
    final_test_cases: List[Dict[str, Any]] = []

    auth_config = state_or_payload.get("auth_config") or {}
    token = auth_config.get("token") or auth_config.get("key")
    auth_type = str(auth_config.get("type") or "bearer").lower()

    for item in target_endpoints:
        ep = item.get("endpoint", {})
        op_id = ep.get("operation_id") or "endpoint_op"
        target_count = item.get("target_count", 1)

        llm_cases = llm_generated_cases_by_op.get(op_id, [])
        merged_cases: List[Dict[str, Any]] = []
        seen_signatures = set()

        # Comprehensive Test Suite: Merge LLM creative lifecycle cases + All Schema boundary/omission/type variations
        for tc in llm_cases:
            sig = _normalize_test_case_key(tc)
            if sig not in seen_signatures:
                seen_signatures.add(sig)
                merged_cases.append(tc)

        # Generate and include all comprehensive QA schema variations (field omissions, type attacks, edge boundaries)
        det_cases = SchemaPayloadGenerator.generate_category_cases(ep, category)
        for dtc in det_cases:
            sig = _normalize_test_case_key(dtc)
            if sig not in seen_signatures:
                seen_signatures.add(sig)
                merged_cases.append(dtc)

        logger.info(
            f"[{category.upper()}_GENERATOR] Operation '{op_id}': Emitting {len(merged_cases)} comprehensive test cases "
            f"({len(llm_cases)} LLM + {len(det_cases)} QA Variations)."
        )

        # Validate and format all merged cases
        for tc_dict in merged_cases:
            if not tc_dict.get("id"):
                tc_dict["id"] = generate_test_case_id(category)
            tc_dict["category"] = category

            # Populate auth headers if auth_config is provided and this is not an auth test omitting auth
            is_omit_auth = tc_dict.get("request", {}).get("omit_auth", False)
            if auth_config and token and category not in ("auth", "authz") and not is_omit_auth:
                req_dict = tc_dict.get("request", {})
                headers_dict = dict(req_dict.get("headers") or {})
                if not any(k.lower() == "authorization" for k in headers_dict):
                    if auth_type == "bearer":
                        headers_dict["Authorization"] = token if str(token).startswith("Bearer ") else f"Bearer {token}"
                    elif auth_type == "api_key":
                        key_name = auth_config.get("name", "X-API-Key")
                        headers_dict[key_name] = str(token)
                    elif auth_type == "basic":
                        headers_dict["Authorization"] = str(token) if str(token).startswith("Basic ") else f"Basic {token}"
                req_dict["headers"] = headers_dict
                tc_dict["request"] = req_dict

            # Standardize max_latency_ms
            expected = tc_dict.get("expected", {})
            if "max_response_time_ms" in expected and "max_latency_ms" not in expected:
                expected["max_latency_ms"] = expected.pop("max_response_time_ms")
            elif "max_latency_ms" not in expected or expected["max_latency_ms"] is None:
                expected["max_latency_ms"] = 5000.0
            tc_dict["expected"] = expected

            try:
                full_model = TestCaseGeneratedModel(**tc_dict)
                final_test_cases.append(full_model.model_dump())
            except Exception as ex:
                logger.warning(f"Validation error for generated test case '{tc_dict.get('name')}': {ex}")
                final_test_cases.append(tc_dict)

    logger.info(f"[{category.upper()}_GENERATOR] Total {len(final_test_cases)} valid test cases assembled for category '{category}'.")

    # 3. Persist generated test cases to MongoDB test_cases collection for regression testing
    if spec_id and final_test_cases:
        try:
            from app.database import get_mongo_db
            db = await get_mongo_db()
            repo = TestCaseRepository(db)
            await repo.save_test_cases(spec_id=spec_id, run_id=run_id, test_cases=final_test_cases)
            logger.info(f"[{category.upper()}_GENERATOR] Saved {len(final_test_cases)} test cases to MongoDB for spec_id='{spec_id}'.")
        except Exception as ex:
            logger.warning(f"[{category.upper()}_GENERATOR] Could not persist test cases to MongoDB (non-fatal): {ex}")

    res_dict: Dict[str, Any] = {"test_cases": final_test_cases}
    if generation_errors:
        res_dict["generation_errors"] = generation_errors
    return res_dict
