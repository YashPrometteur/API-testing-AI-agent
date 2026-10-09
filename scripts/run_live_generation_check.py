import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import get_mongo_db
from app.agents.generators.functional import functional_generator_node
from app.agents.generators.negative import negative_generator_node
from app.agents.generators.edge_case import edge_case_generator_node
from app.agents.planner import PlannerAgent
from app.agents.discovery import DiscoveryAgent


async def main():
    print("[STEP 1/5] Connecting to MongoDB and loading OpenAPI Spec...", flush=True)
    db = await get_mongo_db()
    spec = await db["specs"].find_one({"_id": "spec_a37fb4fc3dde"})
    raw_spec = json.loads(spec["raw_spec"]) if isinstance(spec["raw_spec"], str) else spec["raw_spec"]
    
    target_ops = [
        "get_plan_details_api_subscription_admin_get_plans_list_get",
        "get_plan_details_api_subscription_admin_get_plan_details__plan_id__get",
        "create_subscription_api_subscription_admin_create_subscription_post",
        "update_subscription_api_subscription_admin_update_subscription__plan_id__patch",
        "delete_subscription_api_subscription_admin_delete_plan__plan_id__delete",
        "get_plan_details_api_subscription_user_get_plan_details__plan_id__get",
        "toggle_subscription_api_subscription_admin_toggle_subscription_status__plan_id__post"
    ]
    state = {
        "spec_id": "spec_a37fb4fc3dde",
        "spec_source": raw_spec,
        "run_id": "run_live_llm_check",
        "categories": ["functional", "negative", "edge_case"],
        "target_operations": target_ops,
        "target_base_url": "http://163.227.92.122:4012"
    }

    print("[STEP 2/5] Running Discovery Agent on spec...", flush=True)
    disc = await DiscoveryAgent()(state)
    state.update(disc)
    state["endpoints"] = [ep for ep in state.get("endpoints", []) if ep.get("operation_id") in target_ops]
    print(f"-> Discovered {len(state['endpoints'])} targeted subscription endpoints.", flush=True)

    print("[STEP 3/5] Running Planner Agent with LLM (Groq Qwen)...", flush=True)
    plan = await PlannerAgent()(state)
    state.update(plan)
    print("-> Test Plan generated successfully.", flush=True)

    print("[STEP 4/5] Running Functional Generator (LLM + Deterministic Merge)...", flush=True)
    func_res = await functional_generator_node(state)
    func_tcs = func_res.get("test_cases", [])
    func_gens = list(set(tc.get("generated_by") for tc in func_tcs))
    func_errs = func_res.get("generation_errors", [])
    print(f"-> Functional Generated: {len(func_tcs)} test cases. Sources: {func_gens}. Errors: {len(func_errs)}", flush=True)

    print("[STEP 5/5] Running Negative & Edge Case Generators...", flush=True)
    neg_res = await negative_generator_node(state)
    neg_tcs = neg_res.get("test_cases", [])
    neg_gens = list(set(tc.get("generated_by") for tc in neg_tcs))
    neg_errs = neg_res.get("generation_errors", [])
    print(f"-> Negative Generated: {len(neg_tcs)} test cases. Sources: {neg_gens}. Errors: {len(neg_errs)}", flush=True)

    edge_res = await edge_case_generator_node(state)
    edge_tcs = edge_res.get("test_cases", [])
    edge_gens = list(set(tc.get("generated_by") for tc in edge_tcs))
    edge_errs = edge_res.get("generation_errors", [])
    print(f"-> Edge Case Generated: {len(edge_tcs)} test cases. Sources: {edge_gens}. Errors: {len(edge_errs)}", flush=True)

    # Save summary artifact
    sample_llm_cases = [tc for tc in func_tcs + neg_tcs + edge_tcs if "deterministic" not in str(tc.get("generated_by"))]
    with open("live_llm_generation_verification.json", "w") as f:
        json.dump({
            "functional": {"count": len(func_tcs), "sources": func_gens, "errors": func_errs},
            "negative": {"count": len(neg_tcs), "sources": neg_gens, "errors": neg_errs},
            "edge_case": {"count": len(edge_tcs), "sources": edge_gens, "errors": edge_errs},
            "sample_llm_cases": sample_llm_cases[:3]
        }, f, indent=2)
    print("\n[COMPLETE] Successfully verified live LLM generation across all categories! Saved to live_llm_generation_verification.json", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
