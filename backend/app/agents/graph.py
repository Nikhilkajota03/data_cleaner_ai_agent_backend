"""
Central Multi-Agent Graph Orchestrator (LangGraph).

Assembles the full enterprise compliance pipeline:
1. Guardrail Agent: Local PII detection & reversible token masking (Presidio).
2. Compliance Planner: Schema-aware SQL normalization synthesis (Groq qwen3.8-27b).
3. Safety Judge: LLM-as-a-Judge semantic security rubric & reflection loop routing.
4. Human-in-the-Loop (HITL) Gate: interrupt_before=["execution_worker"].
5. Execution Worker: Cryptographic token validation, PII unmasking, FastMCP transactional execution.
"""

import logging
from typing import Literal, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.app.agents.execution_worker import execution_worker_node
from backend.app.agents.guardrail import guardrail_node
from backend.app.agents.planner import planner_node
from backend.app.agents.judge import judge_node
from backend.app.agents.state import ComplianceState

logger = logging.getLogger(__name__)


# ==============================================================================
# Conditional Routing Logic
# ==============================================================================


def route_after_judge(
    state: ComplianceState,
) -> Literal["planner", "execution_worker", "__end__"]:
    """
    Evaluates judge output to decide whether to:
    1. Loop back to planner for reflection ('planner')
    2. Advance to the HITL gate / execution worker ('execution_worker')
    3. Abort the pipeline on fatal rejection ('__end__')
    """
    status = state.get("status")
    replan_count = state.get("replan_count", 0)

    if status == "PENDING_REASONING" and replan_count < 3:
        logger.info(
            f"Judge requested plan revision. Routing back to Compliance Planner (Attempt {replan_count}/3)."
        )
        return "planner"

    if status == "FAILED":
        logger.warning("Pipeline terminated: Safety Judge permanently rejected plan.")
        return END

    if status in ("PENDING_APPROVAL", "APPROVED"):
        logger.info("Plan approved by Safety Judge / Human. Routing to execution worker.")
        return "execution_worker"

    # Default fallback to end
    return END


# ==============================================================================
# Graph Builder & Factory
# ==============================================================================


def build_compliance_graph() -> StateGraph:
    """
    Constructs the uncompiled StateGraph topology for the compliance pipeline.
    """
    workflow = StateGraph(ComplianceState)

    # 1. Register Multi-Agent Nodes
    workflow.add_node("guardrail", guardrail_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("judge", judge_node)
    workflow.add_node("execution_worker", execution_worker_node)

    # 2. Add Fixed Edges
    workflow.add_edge(START, "guardrail")
    workflow.add_edge("guardrail", "planner")
    workflow.add_edge("planner", "judge")

    # 3. Add Conditional Edge for Reflection Loops & Gate
    workflow.add_conditional_edges(
        "judge",
        route_after_judge,
        {
            "planner": "planner",
            "execution_worker": "execution_worker",
            END: END,
        },
    )

    # 4. Final Edge
    workflow.add_edge("execution_worker", END)

    return workflow


def create_compliance_graph(
    checkpointer: Optional[BaseCheckpointSaver] = None,
) -> CompiledStateGraph:
    """
    Compiles the compliance graph with durable checkpointing and
    an explicit interrupt gate before the execution worker.
    """
    workflow = build_compliance_graph()
    active_checkpointer = checkpointer if checkpointer is not None else MemorySaver()

    # interrupt_before guarantees that the graph pauses automatically
    # at the execution_worker boundary until a human reviews and approves.
    return workflow.compile(
        checkpointer=active_checkpointer,
        interrupt_before=["execution_worker"],
    )


# Default in-memory compiled graph for quick invocations and testing
compliance_graph = create_compliance_graph()
