from typing import Any, Dict, Optional
from langgraph.graph import StateGraph, START, END

from app.agents.discovery import DiscoveryAgent
from app.agents.planner import PlannerAgent
from app.agents.generators import (
    functional_generator_node,
    negative_generator_node,
    edge_case_generator_node,
    security_generator_node,
    auth_generator_node,
    authz_generator_node,
    regression_generator_node,
)
from app.agents.execution.execution_planner import execution_planner_node
from app.agents.execution.execution_agents import execute_batch_node
from app.agents.validation.response_validation_agent import response_validation_node
from app.agents.rca.rca_agent import rca_node
from app.agents.reflection.reflection_agent import reflection_agent_node
from app.agents.guardrail.guardrail_agent import guardrail_agent_node
from app.agents.reporting.reporting_agent import reporting_agent_node
from app.graph.checkpointer import MongoCheckpointSaver
from app.middleware.logging_middleware import instrument_node
from app.graph.routers import (
    fan_out_generators,
    join_test_cases_router,
    fan_out_execution_batches,
    validation_router,
    reflection_router,
)
from app.graph.state import TestRunState


def node_a(state: TestRunState) -> Dict[str, Any]:
    current_count = state.get("reflection_count") or 0
    return {
        "reflection_count": current_count + 1,
        "status": "running"
    }


def node_b(state: TestRunState) -> Dict[str, Any]:
    count = state.get("reflection_count", 0)
    return {
        "status": "completed" if count > 0 else "failed"
    }


def build_noop_graph(
    checkpointer: Optional[MongoCheckpointSaver] = None,
    interrupt_after: Optional[list] = None
):
    """Build minimal 2-node graph for M1 checkpoint interruption verification."""
    builder = StateGraph(TestRunState)
    builder.add_node("node_a", node_a)
    builder.add_node("node_b", node_b)

    builder.add_edge(START, "node_a")
    builder.add_edge("node_a", "node_b")
    builder.add_edge("node_b", END)

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_after=interrupt_after
    )


def join_test_cases_node(state: TestRunState) -> Dict[str, Any]:
    """Join node waiting for all parallel generator branches to complete."""
    return {"status": "running"}


def fail_run_node(state: TestRunState) -> Dict[str, Any]:
    """Failure node reached when all generator branches fail to produce test cases."""
    return {
        "status": "failed",
        "errors": ["All test case generators failed to produce valid test cases"]
    }


def join_execution_node(state: TestRunState) -> Dict[str, Any]:
    """Join node waiting for all parallel batch execution branches to complete."""
    return {"status": "running"}





def guardrail_interrupt_node(state: TestRunState) -> Dict[str, Any]:
    """Guardrail Interrupt Node: Performs dynamic graph interruption when reviews are pending."""
    status = state.get("status")
    pending = state.get("pending_human_review", [])

    if status == "awaiting_human" and pending:
        import logging
        lg = logging.getLogger("graph_build")
        lg.info(f"[GUARDRAIL_INTERRUPT] Interrupting execution. Awaiting review for: {pending}")
        from langgraph.types import interrupt
        interrupt({"run_id": state.get("run_id"), "pending_reviews": pending})

    return {
        "status": "completed"
    }


def build_graph(
    checkpointer: Optional[MongoCheckpointSaver] = None,
    interrupt_after: Optional[list] = None
):
    """Build and compile production LangGraph state graph for Milestone 4."""
    builder = StateGraph(TestRunState)

    discovery_agent = DiscoveryAgent()
    planner_agent = PlannerAgent()

    builder.add_node("discovery", instrument_node("discovery")(discovery_agent))
    builder.add_node("planner", instrument_node("planner")(planner_agent))

    # 7 Generator nodes
    builder.add_node("generate_functional", instrument_node("generate_functional")(functional_generator_node))
    builder.add_node("generate_negative", instrument_node("generate_negative")(negative_generator_node))
    builder.add_node("generate_edge_case", instrument_node("generate_edge_case")(edge_case_generator_node))
    builder.add_node("generate_security", instrument_node("generate_security")(security_generator_node))
    builder.add_node("generate_auth", instrument_node("generate_auth")(auth_generator_node))
    builder.add_node("generate_authz", instrument_node("generate_authz")(authz_generator_node))
    builder.add_node("generate_regression", instrument_node("generate_regression")(regression_generator_node))

    # Join test cases node
    builder.add_node("join_test_cases", instrument_node("join_test_cases")(join_test_cases_node))
    builder.add_node("fail_run", instrument_node("fail_run")(fail_run_node))

    # Execution & Validation nodes
    builder.add_node("execution_planner", instrument_node("execution_planner")(execution_planner_node))
    builder.add_node("execute_batch", instrument_node("execute_batch")(execute_batch_node))
    builder.add_node("join_execution", instrument_node("join_execution")(join_execution_node))
    builder.add_node("response_validation", instrument_node("response_validation")(response_validation_node))
    builder.add_node("report_stub", instrument_node("report_generation")(reporting_agent_node))
    builder.add_node("rca", instrument_node("rca")(rca_node))
    builder.add_node("reflection", instrument_node("reflection")(reflection_agent_node))
    builder.add_node("guardrail", instrument_node("guardrail")(guardrail_agent_node))
    builder.add_node("guardrail_interrupt", instrument_node("guardrail_interrupt")(guardrail_interrupt_node))

    # Edges: Discovery -> Planner -> Generators -> Join
    builder.add_edge(START, "discovery")
    builder.add_edge("discovery", "planner")
    builder.add_conditional_edges("planner", fan_out_generators)

    builder.add_edge("generate_functional", "join_test_cases")
    builder.add_edge("generate_negative", "join_test_cases")
    builder.add_edge("generate_edge_case", "join_test_cases")
    builder.add_edge("generate_security", "join_test_cases")
    builder.add_edge("generate_auth", "join_test_cases")
    builder.add_edge("generate_authz", "join_test_cases")
    builder.add_edge("generate_regression", "join_test_cases")

    # Join test cases router -> execution_planner OR fail_run
    builder.add_conditional_edges(
        "join_test_cases",
        join_test_cases_router,
        {
            "fail_run": "fail_run",
            "proceed_to_execution_planning": "execution_planner"
        }
    )

    # Execution planner -> fan_out_execution_batches -> execute_batch -> join_execution
    builder.add_conditional_edges("execution_planner", fan_out_execution_batches)
    builder.add_edge("execute_batch", "join_execution")

    # Join execution -> response_validation
    builder.add_edge("join_execution", "response_validation")

    # Response validation -> validation_router -> guardrail OR rca
    builder.add_conditional_edges(
        "response_validation",
        validation_router,
        {
            "proceed_to_report": "guardrail",
            "proceed_to_reflection": "rca"
        }
    )

    # RCA -> Reflection
    builder.add_edge("rca", "reflection")

    # Reflection -> reflection_router -> execution_planner OR guardrail
    builder.add_conditional_edges(
        "reflection",
        reflection_router,
        {
            "loop_back_to_execution": "execution_planner",
            "proceed_to_guardrail": "guardrail"
        }
    )

    # Guardrail -> Interrupt Node -> Report Stub
    builder.add_edge("guardrail", "guardrail_interrupt")
    builder.add_edge("guardrail_interrupt", "report_stub")

    builder.add_edge("fail_run", END)
    builder.add_edge("report_stub", END)

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_after=interrupt_after
    )
