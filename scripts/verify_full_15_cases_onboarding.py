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

async def test_full_onboarding_lifecycle():
    # Define the 15 functional test cases across 5 endpoints
    test_cases = [
        # --- Endpoint 1: POST /api/users/request-otp (4 test cases) ---
        {
            "id": "tc_req_otp_1",
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
            "id": "tc_req_otp_2",
            "endpoint_operation_id": "request_otp_route_api_users_request_otp_post",
            "category": "functional",
            "name": "Functional: Minimal required fields - POST /api/users/request-otp",
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
                    "type": "email"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "tc_req_otp_3",
            "endpoint_operation_id": "request_otp_route_api_users_request_otp_post",
            "category": "functional",
            "name": "Functional: Phone type enum - POST /api/users/request-otp",
            "generated_by": "functional_generator_v1",
            "priority": "medium",
            "request": {
                "method": "POST",
                "path": "/api/users/request-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "identifier": "9876543210",
                    "type": "phone",
                    "is_test": True
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "tc_req_otp_4",
            "endpoint_operation_id": "request_otp_route_api_users_request_otp_post",
            "category": "functional",
            "name": "Functional: Optional fields variations - POST /api/users/request-otp",
            "generated_by": "functional_generator_v1",
            "priority": "low",
            "request": {
                "method": "POST",
                "path": "/api/users/request-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "identifier": "dummy@example.com",
                    "type": "email",
                    "lang": "en"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },

        # --- Endpoint 2: POST /api/users/verify-otp (2 test cases) ---
        {
            "id": "tc_ver_otp_1",
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
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "otp": "123456",
                    "type": "email"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "tc_ver_otp_2",
            "endpoint_operation_id": "verify_otp_route_api_users_verify_otp_post",
            "category": "functional",
            "name": "Functional: Minimal required fields - POST /api/users/verify-otp",
            "generated_by": "functional_generator_v1",
            "priority": "high",
            "request": {
                "method": "POST",
                "path": "/api/users/verify-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "otp": "123456"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },

        # --- Endpoint 3: POST /api/users/add-secondary (4 test cases) ---
        {
            "id": "tc_add_sec_1",
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
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "secondary_contact": "9876543210",
                    "type": "phone",
                    "country_code": "+1",
                    "is_test": True
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "tc_add_sec_2",
            "endpoint_operation_id": "add_secondary_contact_route_api_users_add_secondary_post",
            "category": "functional",
            "name": "Functional: Minimal required fields - POST /api/users/add-secondary",
            "generated_by": "functional_generator_v1",
            "priority": "high",
            "request": {
                "method": "POST",
                "path": "/api/users/add-secondary",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "secondary_contact": "9876543210",
                    "type": "phone"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "tc_add_sec_3",
            "endpoint_operation_id": "add_secondary_contact_route_api_users_add_secondary_post",
            "category": "functional",
            "name": "Functional: Custom country code - POST /api/users/add-secondary",
            "generated_by": "functional_generator_v1",
            "priority": "medium",
            "request": {
                "method": "POST",
                "path": "/api/users/add-secondary",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "secondary_contact": "9876543210",
                    "type": "phone",
                    "country_code": "+1",
                    "is_test": True
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "tc_add_sec_4",
            "endpoint_operation_id": "add_secondary_contact_route_api_users_add_secondary_post",
            "category": "functional",
            "name": "Functional: Default country code - POST /api/users/add-secondary",
            "generated_by": "functional_generator_v1",
            "priority": "low",
            "request": {
                "method": "POST",
                "path": "/api/users/add-secondary",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "secondary_contact": "9876543210",
                    "type": "phone",
                    "is_test": True
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },

        # --- Endpoint 4: POST /api/users/verify-secondary-otp (2 test cases) ---
        {
            "id": "tc_ver_sec_1",
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
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "otp": "123456",
                    "type": "phone"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "tc_ver_sec_2",
            "endpoint_operation_id": "verify_secondary_otp_route_api_users_verify_secondary_otp_post",
            "category": "functional",
            "name": "Functional: Minimal required fields - POST /api/users/verify-secondary-otp",
            "generated_by": "functional_generator_v1",
            "priority": "high",
            "request": {
                "method": "POST",
                "path": "/api/users/verify-secondary-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "otp": "123456"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },

        # --- Endpoint 5: POST /api/users/register (3 test cases) ---
        {
            "id": "tc_reg_1",
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
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "first_name": "Alexander",
                    "last_name": "Johnson",
                    "password": "SecurePassword123!",
                    "accepted_terms_id": "64b8f0e9e5162e84ea981e01"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "tc_reg_2",
            "endpoint_operation_id": "register_user_api_users_register_post",
            "category": "functional",
            "name": "Functional: Minimal required fields - POST /api/users/register",
            "generated_by": "functional_generator_v1",
            "priority": "high",
            "request": {
                "method": "POST",
                "path": "/api/users/register",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "first_name": "Alexander",
                    "password": "SecurePassword123!",
                    "accepted_terms_id": "64b8f0e9e5162e84ea981e01"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "tc_reg_3",
            "endpoint_operation_id": "register_user_api_users_register_post",
            "category": "functional",
            "name": "Functional: Different name variant - POST /api/users/register",
            "generated_by": "functional_generator_v1",
            "priority": "medium",
            "request": {
                "method": "POST",
                "path": "/api/users/register",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "first_name": "Alex",
                    "last_name": "J",
                    "password": "SecurePassword123!",
                    "accepted_terms_id": "64b8f0e9e5162e84ea981e01"
                }
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        }
    ]

    # Sort using planner
    ordered_test_cases = sort_functional_workflow_chain(test_cases)

    run_id = "run_live_full_15_testcases"
    batch_payload = {
        "batch_id": "batch_onboarding_15_cases",
        "run_id": run_id,
        "target_base_url": TARGET_BASE_URL,
        "auth_config": {"type": "none"},
        "test_cases": ordered_test_cases
    }

    print(f">>> Executing {len(ordered_test_cases)} functional test cases against live API ({TARGET_BASE_URL})...\n")
    res = await execute_batch_node(batch_payload)
    results = res.get("execution_results", [])

    passed_count = sum(1 for r in results if r.get("status") == "PASS")
    failed_count = sum(1 for r in results if r.get("status") == "FAIL")

    print("\n" + "="*80)
    print(f"RESULTS SUMMARY: {passed_count} PASSED / {failed_count} FAILED out of {len(results)} total ({(passed_count/len(results))*100:.1f}%)")
    print("="*80)

    for i, r in enumerate(results, 1):
        status = r.get('status')
        name = r.get('test_case_id')
        code = r.get('actual_status_code')
        body = r.get('actual_body')
        msg = body.get('message') if isinstance(body, dict) else str(body)[:80]
        print(f"[{i:02d}/15] [{status}] (HTTP {code}) {name} -> {msg}")

    return passed_count, failed_count

if __name__ == "__main__":
    asyncio.run(test_full_onboarding_lifecycle())
