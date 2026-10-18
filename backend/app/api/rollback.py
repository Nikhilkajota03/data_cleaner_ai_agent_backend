"""
Rollback & Reversibility Endpoints.

Handles:
- POST /api/pipeline/{execution_id}/rollback: Invokes FastMCP rollback engine to restore row pre-images.
"""

from datetime import datetime, timezone
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.schemas import RollbackRequest, RollbackResponse
from backend.app.core.config import settings
from backend.app.core.events import ThreadEventBroadcaster
from backend.app.db.models import (
    AgentExecution,
    ExecutionPlan,
    SupervisorThread,
    WorkflowStatus,
)
from backend.app.db.session import get_gov_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["Rollback & Reversibility"])


@router.post(
    "/{execution_id}/rollback",
    response_model=RollbackResponse,
    summary="Revert an executed migration batch using row pre-images",
)
async def rollback_execution(
    execution_id: str,
    request: RollbackRequest = RollbackRequest(),
    gov_db: AsyncSession = Depends(get_gov_db),
) -> RollbackResponse:
    """
    Triggers a 1-click deterministic rollback for a completed or failed execution.
    
    Calls the isolated FastMCP sandbox service (/tools/rollback), which retrieves
    all captured row pre-images from governance_db and re-applies their original
    states to staging_db in reverse chronological order inside a single transaction.
    """
    exec_uuid = uuid.UUID(execution_id) if not isinstance(execution_id, uuid.UUID) else execution_id

    # 1. Verify existence of execution record in governance_db
    exec_stmt = select(AgentExecution).where(AgentExecution.execution_id == exec_uuid)
    exec_res = await gov_db.execute(exec_stmt)
    exec_record = exec_res.scalar_one_or_none()

    if not exec_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Execution audit record '{execution_id}' does not exist.",
        )

    # 2. Call FastMCP Sandbox Service
    fastmcp_url = f"{settings.FASTMCP_SERVICE_URL}/tools/rollback"
    logger.info(f"Invoking FastMCP rollback service at {fastmcp_url} for execution '{execution_id}'.")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                fastmcp_url,
                json={"execution_id": str(exec_uuid)},
            )

        if resp.status_code != status.HTTP_200_OK:
            error_detail = resp.text
            try:
                error_detail = resp.json().get("detail", error_detail)
            except Exception:
                pass
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"FastMCP rollback failed: {error_detail}",
            )

        rollback_data = resp.json()

    except httpx.RequestError as exc:
        logger.error(f"Failed to communicate with FastMCP service: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not reach FastMCP sandbox microservice: {exc}",
        )

    restored_count = (
        rollback_data.get("restored_rows")
        if rollback_data.get("restored_rows") is not None
        else rollback_data.get("restored_count", 0)
    )
    target_tables = (
        [rollback_data["target_table"]]
        if "target_table" in rollback_data
        else rollback_data.get("target_tables", [])
    )

    # Locate parent thread if possible to mark it rolled back
    plan_stmt = select(ExecutionPlan).where(ExecutionPlan.plan_id == exec_record.plan_id)
    plan_res = await gov_db.execute(plan_stmt)
    plan_obj = plan_res.scalar_one_or_none()
    if plan_obj:
        plan_obj.status = WorkflowStatus.ROLLED_BACK
        thread_stmt = select(SupervisorThread).where(SupervisorThread.thread_id == plan_obj.thread_id)
        thread_res = await gov_db.execute(thread_stmt)
        thread_obj = thread_res.scalar_one_or_none()
        if thread_obj:
            thread_obj.current_status = WorkflowStatus.ROLLED_BACK
            thread_obj.updated_at = datetime.now(timezone.utc)
            # Publish event
            await ThreadEventBroadcaster.publish_event(
                str(thread_obj.thread_id),
                "rolled_back",
                {
                    "execution_id": str(exec_uuid),
                    "restored_rows": restored_count,
                    "target_tables": target_tables,
                    "reason": request.reason,
                },
            )

    await gov_db.commit()

    return RollbackResponse(
        execution_id=str(exec_uuid),
        status="ROLLED_BACK",
        restored_rows=restored_count,
        target_tables=target_tables,
        message=f"Rollback completed successfully. {restored_count} rows restored to original pre-image values.",
    )
