import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.agents.execution.execution_agents import execute_batch_node
from app.graph.state import TestCase

LIVE_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbnNlZXVAeW9wbWFpbC5jb20iLCJ1c2VyX2lkIjoiNjk5MmYzNTRkNDdiNmU1Mjg4YmE3YzEzIiwiZXhwIjoxNzg4MjQ2NTE5fQ.aKqtle-FG2QyNauPd0VEFFGd_aiDwZHyXeqPyRo8VdE"
TARGET_BASE_URL = "http://163.227.92.122:4012"

async def main():
    import random
    rand_suffix = random.randint(1000, 9999)

    # 1. LLM-authored create subscription test case (Sample 1)
    tc_create: TestCase = {
        "id": "tc_functional_8a8052b7",
        "endpoint_operation_id": "create_subscription_api_subscription_admin_create_subscription_post",
        "category": "functional",
        "name": "Create a new subscription plan",
        "generated_by": "functional_generator_v1",
        "priority": "medium",
        "request": {
            "method": "POST",
            "path": "/api/subscription/admin/create_subscription",
            "headers": {
                "Authorization": "Bearer dummy_token"  # Dummy token authored by LLM
            },
            "query_params": {},
            "path_params": {},
            "body": {
                "plan_name": f"Enterprise_Live_{rand_suffix}",
                "plan_price": 99.99,
                "plan_duration": "month",
                "plan_feature": [
                    "High Performance",
                    "Dedicated Support"
                ],
                "plan_description": "Comprehensive monthly enterprise subscription plan."
            }
        },
        "expected": {
            "status_code": 200,
            "response_schema_match": True,
            "max_latency_ms": 5000.0
        }
    }

    # 2. LLM-authored get subscription test case with fabricated ID (Sample 2)
    tc_get: TestCase = {
        "id": "tc_functional_5b4fc9b2",
        "endpoint_operation_id": "get_plan_details_api_subscription_admin_get_plan_details__plan_id__get",
        "category": "functional",
        "name": "Retrieve subscription plan by ID",
        "generated_by": "functional_generator_v1",
        "priority": "medium",
        "request": {
            "method": "GET",
            "path": "/api/subscription/admin/get_plan_details/64b8f0e9e5162e84ea981e01",  # Fabricated ID authored by LLM
            "headers": {
                "Authorization": "Bearer dummy_token"  # Dummy token authored by LLM
            },
            "query_params": {},
            "path_params": {
                "plan_id": "64b8f0e9e5162e84ea981e01"
            },
            "body": None
        },
        "expected": {
            "status_code": 200,
            "response_schema_match": True,
            "max_latency_ms": 5000.0
        }
    }

    batch_payload = {
        "batch_id": "batch_live_verification",
        "run_id": "run_verify_auth_and_chaining",
        "target_base_url": TARGET_BASE_URL,
        "auth_config": {
            "type": "bearer",
            "token": LIVE_TOKEN
        },
        "test_cases": [tc_create, tc_get]
    }

    print(">>> Executing batch against live API...", flush=True)
    res = await execute_batch_node(batch_payload)
    results = res.get("execution_results", [])

    print("\n================== EXECUTION RESULTS ==================")
    for r in results:
        print(f"\nTest Case ID: {r.get('test_case_id')}")
        print(f"Status: {r.get('status')}")
        print(f"Actual HTTP Status: {r.get('actual_status_code')}")
        print(f"Latency: {r.get('latency_ms')} ms")
        print(f"Assertion Failures: {r.get('assertion_failures')}")
        print(f"Actual Body: {json.dumps(r.get('actual_body'), indent=2)}")

    with open("live_execution_results.json", "w") as f:
        json.dump(results, f, indent=2)

if __name__ == "__main__":
    asyncio.run(main())
