import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import (
    AgentExecution,
    ExecutionPlan,
    RowPreImage,
    ToolLog,
    WorkflowStatus,
)
from backend.app.db.session import AsyncGovSessionLocal, AsyncStagingSessionLocal


class RollbackExecutor:
    """
    Restores target database rows to their exact prior state using immutable
    pre-images saved in governance_db.row_pre_images.
    """

    async def rollback_execution(
        self,
        execution_id: uuid.UUID,
        primary_key_column: str = "id",
    ) -> dict[str, Any]:
        """
        Reverts an approved mutation by applying stored pre-image snapshots:
          1. Fetches all pre-images for the execution_id from governance_db.
          2. Generates parameterized restore queries for each target row.
          3. Applies restoration inside an atomic transaction on staging_db.
          4. Updates governance status to ROLLED_BACK.
        """
        # Step 1: Fetch all pre-images recorded for this execution
        async with AsyncGovSessionLocal() as gov_session:
            stmt = select(RowPreImage).where(RowPreImage.execution_id == execution_id)
            result = await gov_session.execute(stmt)
            pre_images = result.scalars().all()

            if not pre_images:
                return {
                    "status": "ERROR",
                    "message": f"No pre-images found for execution_id '{execution_id}'. Cannot rollback.",
                    "restored_count": 0,
                }

            # Extract target table
            target_table = pre_images[0].target_table

        # Step 2: Restore rows in staging_db inside an atomic transaction
        restored_count = 0
        async with AsyncStagingSessionLocal() as staging_session:
            async with staging_session.begin():
                for pi in pre_images:
                    state_dict: dict[str, Any] = pi.pre_image_state
                    pk_value = pi.primary_key_value

                    # Build dynamic SET clause excluding the primary key itself
                    set_clauses: list[str] = []
                    bind_params: dict[str, Any] = {"pk_val": pk_value}

                    for col_name, col_value in state_dict.items():
                        if col_name == primary_key_column:
                            continue
                        param_key = f"val_{col_name}"
                        set_clauses.append(f"{col_name} = :{param_key}")

                        # Convert ISO timestamp strings to datetime objects for asyncpg
                        parsed_val = col_value
                        if isinstance(col_value, str):
                            try:
                                parsed_val = datetime.fromisoformat(col_value)
                            except (ValueError, TypeError):
                                parsed_val = col_value

                        bind_params[param_key] = parsed_val

                    set_sql = ", ".join(set_clauses)
                    restore_query = text(
                        f"UPDATE {target_table} SET {set_sql} WHERE {primary_key_column} = :pk_val;"
                    )

                    await staging_session.execute(restore_query, bind_params)
                    restored_count += 1

        # Step 3: Update audit trail in governance_db to ROLLED_BACK
        async with AsyncGovSessionLocal() as gov_session:
            # Update AgentExecution status
            exec_stmt = select(AgentExecution).where(
                AgentExecution.execution_id == execution_id
            )
            exec_res = await gov_session.execute(exec_stmt)
            agent_exec = exec_res.scalar_one_or_none()

            if agent_exec:
                agent_exec.status = WorkflowStatus.ROLLED_BACK

                # Also update parent ExecutionPlan status
                plan_stmt = select(ExecutionPlan).where(
                    ExecutionPlan.plan_id == agent_exec.plan_id
                )
                plan_res = await gov_session.execute(plan_stmt)
                plan = plan_res.scalar_one_or_none()
                if plan:
                    plan.status = WorkflowStatus.ROLLED_BACK

            # Log the rollback event in tool_logs
            rollback_log = ToolLog(
                execution_id=execution_id,
                tool_name="fastmcp_rollback_execution",
                input_parameters={"execution_id": str(execution_id)},
                output_summary=f"Successfully rolled back {restored_count} rows in table '{target_table}'.",
                is_dry_run=False,
                execution_status="ROLLED_BACK",
            )
            gov_session.add(rollback_log)
            await gov_session.commit()

        return {
            "status": "ROLLED_BACK",
            "execution_id": str(execution_id),
            "target_table": target_table,
            "restored_count": restored_count,
        }


rollback_executor = RollbackExecutor()
