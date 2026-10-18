"""
Pipeline Execution & SSE Streaming Endpoints.

Handles:
- POST /api/pipeline/run: Initiates the multi-agent compliance pipeline and runs until the HITL interrupt gate.
- GET /api/pipeline/{thread_id}/stream: Server-Sent Events (SSE) endpoint streaming real-time agent node transitions.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agents.graph import compliance_graph
from backend.app.agents.state import create_initial_state
from backend.app.api.schemas import PipelineRunRequest, PipelineRunResponse
from backend.app.core.events import ThreadEventBroadcaster
from backend.app.core.security import compute_plan_hash
from backend.app.db.models import (
    AgentRole,
    ExecutionPlan,
    MessageHistory,
    SupervisorThread,
    WorkflowStatus,
)
from backend.app.db.session import AsyncGovSessionLocal, get_gov_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["Pipeline Execution & Streaming"])


def _summarize_output(output: dict[str, Any]) -> dict[str, Any]:
    """Helper to extract a concise, safe summary of node outputs for SSE streaming."""
    summary: dict[str, Any] = {}
    if "detected_pii" in output:
        summary["pii_detected_count"] = len(output["detected_pii"])
    if "sanitized_prompt" in output:
        summary["sanitized_preview"] = output["sanitized_prompt"][:100] + "..." if len(output["sanitized_prompt"]) > 100 else output["sanitized_prompt"]
    if "proposed_plan" in output and output["proposed_plan"]:
        summary["plan_summary"] = output["proposed_plan"].get("summary")
        summary["query_count"] = len(output["proposed_plan"].get("queries", []))
    if "judge_evaluation" in output and output["judge_evaluation"]:
        summary["score"] = output["judge_evaluation"].get("score")
        summary["verdict"] = output["judge_evaluation"].get("verdict")
    if "status" in output:
        summary["status"] = output["status"]
    return summary


async def _execute_pipeline_task(thread_id: str, initial_state: dict[str, Any]) -> None:
    """
    Executes LangGraph multi-agent pipeline transitions until interrupted at HITL gate.
    Broadcasts each transition via ThreadEventBroadcaster.
    """
    config = {"configurable": {"thread_id": thread_id}}
    t_uuid = uuid.UUID(thread_id)

    try:
        # Publish start event
        await ThreadEventBroadcaster.publish_event(
            thread_id,
            "pipeline_started",
            {
                "thread_id": thread_id,
                "status": "PENDING_REASONING",
                "original_prompt": initial_state.get("original_prompt"),
            },
        )

        async for chunk in compliance_graph.astream(initial_state, config=config, stream_mode="updates"):
            for node_name, node_output in chunk.items():
                if node_name == "__interrupt__":
                    # HITL Gate reached
                    state_snapshot = await compliance_graph.aget_state(config)
                    values = state_snapshot.values
                    plan_data = values.get("proposed_plan") or {}
                    judge_data = values.get("judge_evaluation") or {}
                    detected_pii = values.get("detected_pii") or []

                    # Persist PENDING_APPROVAL and ExecutionPlan in governance_db
                    async with AsyncGovSessionLocal() as session:
                        t_stmt = select(SupervisorThread).where(SupervisorThread.thread_id == t_uuid)
                        res = await session.execute(t_stmt)
                        thread_obj = res.scalar_one_or_none()
                        if thread_obj:
                            thread_obj.current_status = WorkflowStatus.PENDING_APPROVAL
                            thread_obj.updated_at = datetime.now(timezone.utc)

                        if plan_data:
                            plan_id_str = plan_data.get("plan_id")
                            if isinstance(plan_id_str, uuid.UUID):
                                plan_uuid = plan_id_str
                            else:
                                try:
                                    plan_uuid = uuid.UUID(str(plan_id_str))
                                except (ValueError, TypeError, AttributeError):
                                    plan_uuid = uuid.uuid4()
                            plan_data["plan_id"] = str(plan_uuid)
                            plan_hash = compute_plan_hash(plan_data)

                            # Check if execution plan already exists
                            ep_stmt = select(ExecutionPlan).where(ExecutionPlan.plan_id == plan_uuid)
                            ep_res = await session.execute(ep_stmt)
                            existing_ep = ep_res.scalar_one_or_none()

                            if not existing_ep:
                                new_plan = ExecutionPlan(
                                    plan_id=plan_uuid,
                                    thread_id=t_uuid,
                                    proposed_sql_plan=plan_data,
                                    judge_score=judge_data.get("score"),
                                    judge_feedback=judge_data.get("feedback"),
                                    approval_token_hash=plan_hash,
                                    status=WorkflowStatus.PENDING_APPROVAL,
                                )
                                session.add(new_plan)
                            else:
                                existing_ep.thread_id = t_uuid
                                existing_ep.proposed_sql_plan = plan_data
                                existing_ep.judge_score = judge_data.get("score")
                                existing_ep.judge_feedback = judge_data.get("feedback")
                                existing_ep.approval_token_hash = plan_hash
                                existing_ep.status = WorkflowStatus.PENDING_APPROVAL

                        await session.commit()

                    # Emit HITL paused event
                    await ThreadEventBroadcaster.publish_event(
                        thread_id,
                        "hitl_paused",
                        {
                            "thread_id": thread_id,
                            "status": "PENDING_APPROVAL",
                            "plan_id": plan_data.get("plan_id"),
                            "summary": plan_data.get("summary"),
                            "judge_score": judge_data.get("score"),
                            "verdict": judge_data.get("verdict"),
                            "detected_pii_count": len(detected_pii),
                            "message": "Reasoning complete. Paused at HITL gate awaiting human authorization.",
                        },
                    )
                    return
                else:
                    # Emitted node progress event
                    await ThreadEventBroadcaster.publish_event(
                        thread_id,
                        f"node_{node_name}_completed",
                        {
                            "node": node_name,
                            "summary": _summarize_output(node_output if isinstance(node_output, dict) else {}),
                        },
                    )

    except Exception as ex:
        logger.exception(f"Error during pipeline execution for thread {thread_id}: {ex}")
        async with AsyncGovSessionLocal() as session:
            t_stmt = select(SupervisorThread).where(SupervisorThread.thread_id == t_uuid)
            res = await session.execute(t_stmt)
            thread_obj = res.scalar_one_or_none()
            if thread_obj:
                thread_obj.current_status = WorkflowStatus.FAILED
                thread_obj.updated_at = datetime.now(timezone.utc)
            await session.commit()

        await ThreadEventBroadcaster.publish_event(
            thread_id,
            "failed",
            {
                "thread_id": thread_id,
                "status": "FAILED",
                "error": str(ex),
            },
        )


@router.post(
    "/run",
    response_model=PipelineRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a natural language compliance instruction",
)
async def run_pipeline(
    request: PipelineRunRequest,
    gov_db: AsyncSession = Depends(get_gov_db),
) -> PipelineRunResponse:
    """
    Submits a natural language compliance goal to the multi-agent cluster.
    
    1. Initializes a durable SupervisorThread in governance_db.
    2. Builds initial state.
    3. Runs the agent cluster (Guardrail -> Planner -> Safety Judge).
    4. Automatically halts at the HITL gate (interrupt_before=["execution_worker"])
       for cryptographic human authorization.
    """
    thread_id = str(uuid.uuid4())
    t_uuid = uuid.UUID(thread_id)

    # 1. Record thread and initial message in governance_db
    thread_record = SupervisorThread(
        thread_id=t_uuid,
        user_id=request.user_id,
        master_goal=request.user_prompt,
        current_status=WorkflowStatus.PENDING_REASONING,
    )
    gov_db.add(thread_record)

    msg_record = MessageHistory(
        thread_id=t_uuid,
        role=AgentRole.SUPERVISOR,
        content=request.user_prompt,
    )
    gov_db.add(msg_record)
    await gov_db.commit()

    # 2. Build state
    initial_state = create_initial_state(
        user_prompt=request.user_prompt,
        user_id=request.user_id,
        thread_id=thread_id,
    )

    # 3. Synchronous vs Asynchronous execution mode
    if request.wait_for_pause:
        await _execute_pipeline_task(thread_id, initial_state)
        # Fetch resulting state to populate response
        config = {"configurable": {"thread_id": thread_id}}
        current_state = await compliance_graph.aget_state(config)
        values = current_state.values if current_state else {}
        detected_pii = values.get("detected_pii", [])
        curr_status = values.get("status", "PENDING_APPROVAL")

        return PipelineRunResponse(
            thread_id=thread_id,
            status=curr_status,
            message="Reasoning completed successfully. Pipeline paused at HITL gate for human compliance approval.",
            detected_pii_count=len(detected_pii),
        )
    else:
        # Launch asynchronously in background task
        asyncio.create_task(_execute_pipeline_task(thread_id, initial_state))
        return PipelineRunResponse(
            thread_id=thread_id,
            status="PENDING_REASONING",
            message="Pipeline reasoning initiated in background. Stream live transitions via /api/pipeline/{thread_id}/stream.",
            detected_pii_count=0,
        )


@router.get(
    "/{thread_id}/stream",
    summary="Stream real-time agent node transitions via Server-Sent Events (SSE)",
)
async def stream_pipeline_events(
    thread_id: str,
    timeout: float = Query(default=60.0, ge=5.0, le=300.0, description="Max stream duration in seconds"),
) -> EventSourceResponse:
    """
    Subscribes to live Server-Sent Events (SSE) for the specified pipeline thread.
    
    Streams events as agents transition:
    - pipeline_started
    - node_guardrail_completed (PII masked)
    - node_planner_completed (SQL synthesized)
    - node_judge_completed (Safety scored)
    - hitl_paused (Awaiting human approval)
    - node_execution_worker_completed (Executed in FastMCP)
    - completed / failed
    """
    event_generator = ThreadEventBroadcaster.stream_events(thread_id=thread_id, timeout_seconds=timeout)
    return EventSourceResponse(event_generator)
