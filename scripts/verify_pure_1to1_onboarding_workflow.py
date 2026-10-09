import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.agents.execution.execution_agents import execute_batch_node
from app.agents.execution.execution_planner import sort_functional_workflow_chain
from app.graph.state import TestCase

TARGET_BASE_URL = "http://163.227.92.122:4012"

async def test_pure_1to1_sequential_workflow():
    # 15 test cases (duplicates & variants) passed in - Planner should deduplicate onboarding into exactly 1-to-1 chain
    test_cases = [
        # --- Endpoint 1: POST /api/users/request-otp ---
        {
            "id": "tc_req_otp_full",
            "endpoint_operation_id": "request_otp_route_api_users_request_otp_post",
            "category": "functional",
            "name": "Functional: Full valid payload - POST /api/users/request-otp",
            "generated_by": "functional_generator_v1",
            "priority": "high",
            "request": {
                "method": "POST",
                "path": "/api/users/request-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "identifier": "dummy@example.com",
                    "type": "email",
                    "lang": "en",
                    "is_test": True
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "tc_req_otp_minimal",
            "endpoint_operation_id": "request_otp_route_api_users_request_otp_post",
            "category": "functional",
            "name": "Functional: Minimal required fields - POST /api/users/request-otp",
            "generated_by": "functional_generator_v1",
            "priority": "medium",
            "request": {
                "method": "POST",
                "path": "/api/users/request-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"identifier": "dummy@example.com", "type": "email"}
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },

        # --- Endpoint 2: POST /api/users/verify-otp ---
        {
            "id": "tc_ver_otp_full",
            "endpoint_operation_id": "verify_otp_route_api_users_verify_otp_post",
            "category": "functional",
            "name": "Functional: Full valid payload - POST /api/users/verify-otp",
            "generated_by": "functional_generator_v1",
            "priority": "high",
            "request": {
                "method": "POST",
                "path": "/api/users/verify-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "session_id": "placeholder_sess",
                    "otp": "placeholder_otp",
                    "lang": "en"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },

        # --- Endpoint 3: POST /api/users/add-secondary ---
        {
            "id": "tc_add_sec_full",
            "endpoint_operation_id": "add_secondary_contact_route_api_users_add_secondary_post",
            "category": "functional",
            "name": "Functional: Full valid payload - POST /api/users/add-secondary",
            "generated_by": "functional_generator_v1",
            "priority": "high",
            "request": {
                "method": "POST",
                "path": "/api/users/add-secondary",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "session_id": "placeholder_sess",
                    "secondary_contact": "9876543210",
                    "type": "phone",
                    "country_code": "+1",
                    "is_test": True
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },

        # --- Endpoint 4: POST /api/users/verify-secondary-otp ---
        {
            "id": "tc_ver_sec_full",
            "endpoint_operation_id": "verify_secondary_otp_route_api_users_verify_secondary_otp_post",
            "category": "functional",
            "name": "Functional: Full valid payload - POST /api/users/verify-secondary-otp",
            "generated_by": "functional_generator_v1",
            "priority": "high",
            "request": {
                "method": "POST",
                "path": "/api/users/verify-secondary-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "session_id": "placeholder_sess",
                    "otp": "placeholder_otp"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },

        # --- Endpoint 5: POST /api/users/register ---
        {
            "id": "tc_reg_full",
            "endpoint_operation_id": "register_user_api_users_register_post",
            "category": "functional",
            "name": "Functional: Full valid payload - POST /api/users/register",
            "generated_by": "functional_generator_v1",
            "priority": "high",
            "request": {
                "method": "POST",
                "path": "/api/users/register",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "session_id": "placeholder_sess",
                    "first_name": "Alexander",
                    "last_name": "Johnson",
                    "password": "SecurePassword123!",
                    "accepted_terms_id": "64b8f0e9e5162e84ea981e01"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        }
    ]

    # Plan and deduplicate
    ordered_test_cases = sort_functional_workflow_chain(test_cases)
    print(f"\n[PLANNER OUTPUT] Ordered pure workflow contains {len(ordered_test_cases)} steps:")
    for i, tc in enumerate(ordered_test_cases, 1):
        print(f"  Step {i}: {tc['request']['method']} {tc['request']['path']} ({tc['name']})")

    run_id = "run_live_pure_1to1_workflow"
    batch_payload = {
        "batch_id": "batch_pure_workflow_1",
        "run_id": run_id,
        "target_base_url": TARGET_BASE_URL,
        "auth_config": {"type": "none"},
        "test_cases": ordered_test_cases
    }

    print(f"\n>>> Executing pure 1-to-1 sequential workflow against {TARGET_BASE_URL}...\n")
    res = await execute_batch_node(batch_payload)
    results = res.get("execution_results", [])

    print("="*80)
    print("PURE 1-TO-1 WORKFLOW RESULTS:")
    print("="*80)
    for i, r in enumerate(results, 1):
        status = r.get('actual_status_code')
        body = r.get('actual_body')
        msg = body.get('message') if isinstance(body, dict) else str(body)[:80]
        data_preview = body.get("data") if isinstance(body, dict) else {}
        print(f"Step {i:02d}: HTTP {status} -> {msg} | Data: {str(data_preview)[:80]}")

if __name__ == "__main__":
    asyncio.run(test_pure_1to1_sequential_workflow())
