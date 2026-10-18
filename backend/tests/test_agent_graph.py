"""
Unit tests for Step 16: Multi-Agent Graph Assembly & Checkpointing.
Validates StateGraph compilation, conditional reflection routing,
HITL pause at interrupt_before=["execution_worker"], and workflow resumption.
"""

import uuid
import pytest
from langgraph.checkpoint.memory import MemorySaver

from backend.app.agents.graph import (
    build_compliance_graph,
    create_compliance_graph,
    route_after_judge,
)
from backend.app.agents.state import ComplianceState, create_initial_state
from backend.app.core.security import compute_plan_hash, generate_approval_token


def test_graph_topology_nodes():
    """Asserts that all multi-agent nodes and edges are registered."""
    workflow = build_compliance_graph()
    node_keys = set(workflow.nodes.keys())

    assert "guardrail" in node_keys
    assert "planner" in node_keys
    assert "judge" in node_keys
    assert "execution_worker" in node_keys


def test_route_after_judge_matrix():
    """Validates conditional routing logic after judge evaluation."""
    # Scenario A: Reflection loop triggered
    state_replan: ComplianceState = {
        "status": "PENDING_REASONING",
        "replan_count": 1,
    }  # type: ignore
    assert route_after_judge(state_replan) == "planner"

    # Scenario B: Pipeline failed
    state_failed: ComplianceState = {
        "status": "FAILED",
        "replan_count": 3,
    }  # type: ignore
    assert route_after_judge(state_failed) == "__end__"

    # Scenario C: Approved, ready for HITL review
    state_approved: ComplianceState = {
        "status": "PENDING_APPROVAL",
        "replan_count": 0,
    }  # type: ignore
    assert route_after_judge(state_approved) == "execution_worker"


@pytest.mark.asyncio
async def test_graph_interrupt_at_hitl_gate():
    """
    Asserts that the multi-agent graph runs Guardrail -> Planner -> Judge,
    and PAUSES execution right before execution_worker.
    """
    checkpointer = MemorySaver()
    graph = create_compliance_graph(checkpointer=checkpointer)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = create_initial_state(
        user_prompt="Standardize German country names in the clients table to 'DE'",
        thread_id=thread_id,
    )

    # 1. Run graph until it hits the interrupt gate
    final_output = await graph.ainvoke(initial_state, config=config)

    # 2. Assert pipeline paused at PENDING_APPROVAL
    assert final_output["status"] == "PENDING_APPROVAL"
    assert final_output["proposed_plan"] is not None
    assert final_output["judge_evaluation"] is not None
    assert final_output["judge_evaluation"]["passed"] is True

    # 3. Inspect checkpoint: next node to execute MUST be execution_worker
    graph_state = await graph.aget_state(config)
    assert "execution_worker" in graph_state.next

    # 4. Assert execution worker has NOT run yet
    assert final_output.get("execution_result") is None


@pytest.mark.asyncio
async def test_graph_resumption_after_human_approval():
    """
    Asserts that after human sign-off (with cryptographic approval token),
    the graph resumes from the checkpointer and completes execution.
    """
    checkpointer = MemorySaver()
    graph = create_compliance_graph(checkpointer=checkpointer)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = create_initial_state(
        user_prompt="Normalize country code for client with full name Hans Gruber to DE",
        thread_id=thread_id,
    )

    # 1. Run until paused at HITL gate
    paused_output = await graph.ainvoke(initial_state, config=config)
    assert paused_output["status"] == "PENDING_APPROVAL"

    # 2. Simulate Human-in-the-Loop review and cryptographic approval
    plan_dict = paused_output["proposed_plan"]
    plan_hash = compute_plan_hash(plan_dict)
    valid_token = generate_approval_token(
        thread_id=thread_id,
        plan_id=plan_dict["plan_id"],
        plan_hash=plan_hash,
    )

    # 3. Update checkpoint state with the human's approval token as the judge node
    await graph.aupdate_state(
        config,
        {
            "approval_token": valid_token,
            "approved_by": "compliance_director@enterprise.com",
            "status": "APPROVED",
        },
        as_node="judge",
    )

    # 4. Resume execution from checkpoint
    await graph.ainvoke(None, config=config)

    # 5. Inspect final resumed checkpoint state
    final_state = await graph.aget_state(config)
    final_values = final_state.values

    assert final_values["status"] == "COMPLETED"
    assert final_values.get("execution_result") is not None
    assert final_values["execution_result"]["status"] == "COMPLETED"
    assert len(final_state.next) == 0
