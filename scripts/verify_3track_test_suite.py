import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.agents.execution.execution_agents import execute_batch_node
from app.agents.execution.execution_planner import sort_functional_workflow_chain, sort_test_cases_by_dependency
from app.tools.assertion_engine import AssertionEngineTool
from app.graph.state import TestCase

TARGET_BASE_URL = "http://163.227.92.122:4012"

async def run_3track_comprehensive_suite():
    print("="*90)
    print("STARTING 3-TRACK COMPREHENSIVE ONBOARDING & VARIATION TEST SUITE")
    print("="*90)

    # 1. TRACK 1: Pure 1-to-1 Sequential Happy Path (1 Shared Session)
    track_1_cases = [
        {
            "id": "t1_req_otp",
            "endpoint_operation_id": "request_otp_route_api_users_request_otp_post",
            "category": "functional",
            "name": "Track 1 [01/06]: Request OTP (Primary Email) - POST /api/users/request-otp",
            "request": {
                "method": "POST",
                "path": "/api/users/request-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"identifier": "dummy@example.com", "type": "email", "lang": "en", "is_test": True}
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "t1_ver_otp",
            "endpoint_operation_id": "verify_otp_route_api_users_verify_otp_post",
            "category": "functional",
            "name": "Track 1 [02/06]: Verify Primary OTP - POST /api/users/verify-otp",
            "request": {
                "method": "POST",
                "path": "/api/users/verify-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"session_id": "placeholder_sess", "otp": "placeholder_otp", "lang": "en"}
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "t1_add_sec",
            "endpoint_operation_id": "add_secondary_contact_route_api_users_add_secondary_post",
            "category": "functional",
            "name": "Track 1 [03/06]: Add Secondary Contact (Phone) - POST /api/users/add-secondary",
            "request": {
                "method": "POST",
                "path": "/api/users/add-secondary",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"session_id": "placeholder_sess", "secondary_contact": "9876543210", "type": "phone", "country_code": "+1", "is_test": True}
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "t1_ver_sec",
            "endpoint_operation_id": "verify_secondary_otp_route_api_users_verify_secondary_otp_post",
            "category": "functional",
            "name": "Track 1 [04/06]: Verify Secondary OTP - POST /api/users/verify-secondary-otp",
            "request": {
                "method": "POST",
                "path": "/api/users/verify-secondary-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"session_id": "placeholder_sess", "otp": "placeholder_otp"}
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "t1_reg",
            "endpoint_operation_id": "register_user_api_users_register_post",
            "category": "functional",
            "name": "Track 1 [05/06]: Complete User Registration - POST /api/users/register",
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
        },
        {
            "id": "t1_verify_state",
            "endpoint_operation_id": "request_otp_route_api_users_request_otp_post",
            "category": "functional",
            "name": "Track 1 [06/06]: Verify Final User State - POST /api/users/request-otp (Existing Registered User State)",
            "request": {
                "method": "POST",
                "path": "/api/users/request-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"identifier": "dummy@example.com", "type": "email", "lang": "en", "is_test": True}
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        }
    ]

    # 2. TRACK 2: Field Boundary & Business Variation Matrix
    track_2_cases = [
        {
            "id": "t2_email_tag",
            "endpoint_operation_id": "request_otp_route_api_users_request_otp_post",
            "category": "functional",
            "name": "Track 2 [01/04]: Email with Subdomain - POST /api/users/request-otp",
            "request": {
                "method": "POST",
                "path": "/api/users/request-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"identifier": "alex.qa.test@sub.example.org", "type": "email", "is_test": True}
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "t2_phone_in",
            "endpoint_operation_id": "request_otp_route_api_users_request_otp_post",
            "category": "functional",
            "name": "Track 2 [02/04]: Phone with India (+91) Country Code - POST /api/users/request-otp",
            "request": {
                "method": "POST",
                "path": "/api/users/request-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"identifier": "918765432109", "type": "phone", "country_code": "+91", "is_test": True}
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "t2_name_min_len",
            "endpoint_operation_id": "register_user_api_users_register_post",
            "category": "functional",
            "name": "Track 2 [03/04]: Minimum Valid Name Boundary (2 chars: 'Al Li') - POST /api/users/register",
            "request": {
                "method": "POST",
                "path": "/api/users/register",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"session_id": "sess_boundary", "first_name": "Al", "last_name": "Li", "accepted_terms_id": "64b8f0e9e5162e84ea981e01"}
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "t2_name_compound",
            "endpoint_operation_id": "register_user_api_users_register_post",
            "category": "functional",
            "name": "Track 2 [04/04]: Compound Name ('Mary Jane') - POST /api/users/register",
            "request": {
                "method": "POST",
                "path": "/api/users/register",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"session_id": "sess_boundary", "first_name": "Mary Jane", "last_name": "Alexander Smith", "accepted_terms_id": "64b8f0e9e5162e84ea981e01"}
            },
            "expected": {"status_code": 200, "response_schema_match": True, "max_latency_ms": 5000.0}
        }
    ]

    # 3. TRACK 3: Real Business-Logic Negative Tests
    track_3_cases = [
        {
            "id": "t3_wrong_otp",
            "endpoint_operation_id": "verify_otp_route_api_users_verify_otp_post",
            "category": "negative",
            "name": "Track 3 [01/04]: Wrong 6-Digit OTP ('999999') - POST /api/users/verify-otp",
            "request": {
                "method": "POST",
                "path": "/api/users/verify-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"session_id": "dummy_sess", "otp": "999999"}
            },
            "expected": {"status_code": 400, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "t3_expired_sess",
            "endpoint_operation_id": "verify_otp_route_api_users_verify_otp_post",
            "category": "negative",
            "name": "Track 3 [02/04]: Expired/Nonexistent Session ID - POST /api/users/verify-otp",
            "request": {
                "method": "POST",
                "path": "/api/users/verify-otp",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"session_id": "00000000-0000-0000-0000-000000000000", "otp": "123456"}
            },
            "expected": {"status_code": 400, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "t3_short_pwd",
            "endpoint_operation_id": "register_user_api_users_register_post",
            "category": "negative",
            "name": "Track 3 [03/04]: Password Shorter Than 8 Chars ('123') - POST /api/users/register",
            "request": {
                "method": "POST",
                "path": "/api/users/register",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"session_id": "dummy_sess", "first_name": "Alexander", "last_name": "Johnson", "password": "123", "accepted_terms_id": "64b8f0e9e5162e84ea981e01"}
            },
            "expected": {"status_code": 422, "response_schema_match": True, "max_latency_ms": 5000.0}
        },
        {
            "id": "t3_missing_last_name",
            "endpoint_operation_id": "register_user_api_users_register_post",
            "category": "negative",
            "name": "Track 3 [04/04]: Missing Required Field 'last_name' - POST /api/users/register",
            "request": {
                "method": "POST",
                "path": "/api/users/register",
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": {"session_id": "dummy_sess", "first_name": "Alexander", "password": "SecurePassword123!", "accepted_terms_id": "64b8f0e9e5162e84ea981e01"}
            },
            "expected": {"status_code": 422, "response_schema_match": True, "max_latency_ms": 5000.0}
        }
    ]

    all_cases = track_1_cases + track_2_cases + track_3_cases

    run_id = "run_live_3track_full_demonstration"
    batch_payload = {
        "batch_id": "batch_3track_full",
        "run_id": run_id,
        "target_base_url": TARGET_BASE_URL,
        "auth_config": {"type": "none"},
        "test_cases": all_cases
    }

    print(f"\nExecuting {len(all_cases)} test cases across all 3 tracks...\n")
    res = await execute_batch_node(batch_payload)
    results = res.get("execution_results", [])

    print("\n" + "="*90)
    print("EVALUATING RESULTS WITH DEEP BUSINESS ASSERTIONS")
    print("="*90)

    for i, (tc, r) in enumerate(zip(all_cases, results), 1):
        status_code = r.get("actual_status_code")
        body = r.get("actual_body")
        headers = r.get("actual_headers", {})
        latency = r.get("latency_ms", 0.0)

        # Run Deep Business Assertions
        eval_res = AssertionEngineTool.evaluate(
            actual_status_code=status_code,
            actual_headers=headers,
            actual_body=body,
            latency_ms=latency,
            expected=tc.get("expected", {}),
            schema_valid=True,
            request=tc.get("request")
        )

        passed = eval_res.data.get("passed", False)
        failures = eval_res.data.get("assertion_failures", [])
        status_str = "PASS" if passed else "FAIL"

        msg = body.get("message") if isinstance(body, dict) else str(body)[:60]
        print(f"[{i:02d}/{len(all_cases):02d}] [{status_str}] (HTTP {status_code}) {tc['name']}")
        print(f"       -> Response: {msg}")
        if failures:
            print(f"       -> Assertion Failures: {failures}")

if __name__ == "__main__":
    asyncio.run(run_3track_comprehensive_suite())
