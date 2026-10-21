"""
Cryptographic Human-in-the-Loop (HITL) Approval & Rejection Endpoints.

Handles:
- GET /api/pipeline/{thread_id}/pending: Inspects the paused plan, computes SHA-256 digest, and issues an HMAC-SHA256 JWT approval token.
- POST /api/pipeline/approve: Cryptographically validates the approval token and resumes execution across the HITL gate.
- POST /api/pipeline/reject: Records human rejection reason and permanently terminates the workflow.
"""

from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agents.graph import compliance_graph
from backend.app.api.schemas import (
    ApprovePlanRequest,
    ApprovePlanResponse,
    PendingApprovalResponse,
    RejectPlanRequest,
    RejectPlanResponse,
)
from backend.app.core.events import ThreadEventBroadcaster
from backend.app.core.security import (
    ExpiredApprovalTokenError,
    InvalidApprovalTokenError,
    TamperedPlanError,
    compute_plan_hash,
    generate_approval_token,
    verify_approval_token,
)
from backend.app.db.models import (
    AgentRole,
    ExecutionPlan,
    MessageHistory,
    SupervisorThread,
    WorkflowStatus,
)
from backend.app.db.session import get_gov_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["Cryptographic HITL Governance"])


@router.get(
    "/{thread_id}/pending",
    response_model=PendingApprovalResponse,
    summary="Inspect paused plan and retrieve cryptographic approval token",
)
async def get_pending_plan(
    thread_id: str,
    gov_db: AsyncSession = Depends(get_gov_db),
) -> PendingApprovalResponse:
    """
    Retrieves the proposed migration plan currently paused at the HITL interrupt gate.
    
    Computes a deterministic SHA-256 fingerprint of the plan and issues a cryptographically
    signed HMAC-SHA256 JWT approval token that must be presented back upon approval.
    """
    config = {"configurable": {"thread_id": thread_id}}
    state_snapshot = await compliance_graph.aget_state(config)

    if not state_snapshot or not state_snapshot.values:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active or paused workflow found for thread ID '{thread_id}'.",
        )

    values = state_snapshot.values
    proposed_plan = values.get("proposed_plan")

    if not proposed_plan:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Thread '{thread_id}' does not have a proposed plan ready for review.",
        )

    plan_id = proposed_plan.get("plan_id", str(uuid.uuid4()))
    plan_hash = compute_plan_hash(proposed_plan)

    # Issue cryptographic JWT token
    approval_token = generate_approval_token(
        thread_id=thread_id,
        plan_id=plan_id,
        plan_hash=plan_hash,
    )

    # Ensure plan is updated in DB
    try:
        p_uuid = uuid.UUID(str(plan_id)) if not isinstance(plan_id, uuid.UUID) else plan_id
    except (ValueError, TypeError, AttributeError):
        p_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, str(plan_id) if plan_id else str(uuid.uuid4()))

    stmt = select(ExecutionPlan).where(ExecutionPlan.plan_id == p_uuid)
    res = await gov_db.execute(stmt)
    plan_obj = res.scalar_one_or_none()
    if plan_obj:
        plan_obj.approval_token_hash = plan_hash
        await gov_db.commit()

    return PendingApprovalResponse(
        thread_id=thread_id,
        plan_id=str(plan_id),
        plan_hash=plan_hash,
        approval_token=approval_token,
        status=values.get("status", "PENDING_APPROVAL"),
        proposed_plan=proposed_plan,
        judge_evaluation=values.get("judge_evaluation"),
        detected_pii=values.get("detected_pii", []),
    )


@router.post(
    "/approve",
    response_model=ApprovePlanResponse,
    summary="Cryptographically approve and execute migration plan",
)
async def approve_plan(
    request: ApprovePlanRequest,
    gov_db: AsyncSession = Depends(get_gov_db),
) -> ApprovePlanResponse:
    """
    Validates the cryptographic HMAC-SHA256 JWT approval token and verifies that the
    proposed plan has not been tampered with since issuance.
    
    Upon verification, updates LangGraph state to 'APPROVED' and resumes execution
    past the interrupt gate into the Execution Worker.
    """
    config = {"configurable": {"thread_id": request.thread_id}}
    state_snapshot = await compliance_graph.aget_state(config)

    if not state_snapshot or not state_snapshot.values:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workflow state found for thread ID '{request.thread_id}'.",
        )

    values = state_snapshot.values
    proposed_plan = values.get("proposed_plan")

    if not proposed_plan:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot approve workflow: No proposed plan exists in current state.",
        )

    plan_id = proposed_plan.get("plan_id", "")
    current_plan_hash = compute_plan_hash(proposed_plan)

    # 1. Cryptographic token verification
    try:
        verify_approval_token(
            token=request.approval_token,
            expected_plan_id=plan_id,
            current_plan_hash=current_plan_hash,
        )
    except ExpiredApprovalTokenError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except (InvalidApprovalTokenError, TamperedPlanError) as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Token validation failed: {e}")

    now_iso = datetime.now(timezone.utc).isoformat()

    # 2. Update state to APPROVED with approval metadata
    await compliance_graph.aupdate_state(
        config,
        {
            "status": "APPROVED",
            "approval_token": request.approval_token,
            "approved_by": request.approver_id,
            "approved_at": now_iso,
        },
        as_node="judge",
    )

    # 3. Resume LangGraph execution past the interrupt gate
    logger.info(f"Resuming graph past HITL gate for thread '{request.thread_id}'.")
    resume_config = {
        "configurable": {"thread_id": request.thread_id},
        "tags": ["hitl_resumption", "execution_worker"],
        "metadata": {
            "thread_id": request.thread_id,
            "approver_id": request.approver_id,
        },
        "run_name": f"HITL_Resumption_{request.thread_id[:8]}",
    }
    final_state = await compliance_graph.ainvoke(None, config=resume_config)

    exec_result = final_state.get("execution_result") or {}
    exec_status = exec_result.get("status", "COMPLETED")
    t_uuid = uuid.UUID(request.thread_id)

    # 4. Synchronize database statuses
    thread_stmt = select(SupervisorThread).where(SupervisorThread.thread_id == t_uuid)
    t_res = await gov_db.execute(thread_stmt)
    thread_obj = t_res.scalar_one_or_none()
    if thread_obj:
        thread_obj.current_status = (
            WorkflowStatus.COMPLETED if exec_status == "COMPLETED" else WorkflowStatus.FAILED
        )
        thread_obj.updated_at = datetime.now(timezone.utc)

    try:
        p_uuid = uuid.UUID(str(plan_id)) if not isinstance(plan_id, uuid.UUID) else plan_id
    except (ValueError, TypeError, AttributeError):
        p_uuid = None

    if p_uuid:
        plan_stmt = select(ExecutionPlan).where(ExecutionPlan.plan_id == p_uuid)
        p_res = await gov_db.execute(plan_stmt)
        plan_obj = p_res.scalar_one_or_none()
        if plan_obj:
            plan_obj.status = WorkflowStatus.APPROVED
            plan_obj.approved_by = request.approver_id
            plan_obj.approved_at = datetime.fromisoformat(now_iso)

    # Add message history log
    msg_record = MessageHistory(
        thread_id=t_uuid,
        role=AgentRole.EXECUTION_WORKER,
        content=f"Plan executed by {request.approver_id}. Status: {exec_status}. Rows affected: {exec_result.get('total_affected_rows', 0)}.",
    )
    gov_db.add(msg_record)
    await gov_db.commit()

    # 5. Broadcast completion event
    query_results = exec_result.get("query_results", [])
    await ThreadEventBroadcaster.publish_event(
        request.thread_id,
        "completed" if exec_status == "COMPLETED" else "failed",
        {
            "thread_id": request.thread_id,
            "status": exec_status,
            "execution_id": exec_result.get("execution_id"),
            "total_affected_rows": exec_result.get("total_affected_rows", 0),
            "pre_images_captured": exec_result.get("pre_images_captured", 0),
            "payload_s3_uri": exec_result.get("payload_s3_uri"),
            "query_results": query_results,
        },
    )

    return ApprovePlanResponse(
        thread_id=request.thread_id,
        execution_id=exec_result.get("execution_id"),
        status=exec_status,
        total_affected_rows=exec_result.get("total_affected_rows", 0),
        pre_images_captured=exec_result.get("pre_images_captured", 0),
        payload_s3_uri=exec_result.get("payload_s3_uri"),
        query_results=query_results,
        message=f"Plan successfully verified and executed. {exec_result.get('total_affected_rows', 0)} rows modified.",
    )


@router.post(
    "/reject",
    response_model=RejectPlanResponse,
    summary="Reject proposed compliance plan",
)
async def reject_plan(
    request: RejectPlanRequest,
    gov_db: AsyncSession = Depends(get_gov_db),
) -> RejectPlanResponse:
    """
    Rejects the pending compliance plan, halts further execution,
    and records the human rejection rationale in governance_db.
    """
    config = {"configurable": {"thread_id": request.thread_id}}

    # 1. Update LangGraph state to REJECTED
    await compliance_graph.aupdate_state(
        config,
        {
            "status": "REJECTED",
            "error_message": f"Rejected by {request.rejected_by}: {request.rejection_reason}",
        },
    )

    # 2. Update database records
    t_uuid = uuid.UUID(request.thread_id)
    thread_stmt = select(SupervisorThread).where(SupervisorThread.thread_id == t_uuid)
    t_res = await gov_db.execute(thread_stmt)
    thread_obj = t_res.scalar_one_or_none()
    if thread_obj:
        thread_obj.current_status = WorkflowStatus.REJECTED
        thread_obj.updated_at = datetime.now(timezone.utc)

    # Update ExecutionPlan status if exists
    plan_stmt = select(ExecutionPlan).where(ExecutionPlan.thread_id == t_uuid)
    p_res = await gov_db.execute(plan_stmt)
    plan_obj = p_res.scalars().first()
    if plan_obj:
        plan_obj.status = WorkflowStatus.REJECTED

    msg_record = MessageHistory(
        thread_id=t_uuid,
        role=AgentRole.SUPERVISOR,
        content=f"Plan rejected by {request.rejected_by}. Rationale: {request.rejection_reason}",
    )
    gov_db.add(msg_record)
    await gov_db.commit()

    # 3. Broadcast rejection event
    await ThreadEventBroadcaster.publish_event(
        request.thread_id,
        "rejected",
        {
            "thread_id": request.thread_id,
            "status": "REJECTED",
            "rejected_by": request.rejected_by,
            "rejection_reason": request.rejection_reason,
        },
    )

    return RejectPlanResponse(
        thread_id=request.thread_id,
        status="REJECTED",
        rejection_reason=request.rejection_reason,
        message=f"Plan rejected by {request.rejected_by}. Workflow terminated.",
    )
