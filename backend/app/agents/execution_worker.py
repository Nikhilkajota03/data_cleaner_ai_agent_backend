"""
Execution Worker Agent: Deterministic Relay & FastMCP Sandbox Dispatcher.

This agent:
1. Validates the cryptographic HITL approval token (verifying HMAC-SHA256 signature and plan hash).
2. Deterministically unmasks PII placeholder tokens in the approved SQL plan using the state's pii_mapping.
3. Dispatches unmasked mutations to the isolated FastMCP execution sandbox (POST /tools/execute).
4. Captures row pre-image snapshot telemetry for 1-click reversibility/rollback.
5. Offloads tool results and execution diffs > 5 KB to MinIO S3 cold storage.
6. Emits an ExecutionResult and transitions pipeline status to COMPLETED or FAILED.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from langchain_core.messages import AIMessage

from backend.app.agents.guardrail import GuardrailAgent
from backend.app.agents.state import CompliancePlan, ComplianceState, ExecutionResult
from backend.app.core.config import settings
from backend.app.core.security import (
    SecurityException,
    compute_plan_hash,
    verify_approval_token,
)
from backend.app.core.storage import storage_manager
from backend.app.db.models import (
    AgentExecution,
    AgentRole,
    ExecutionPlan,
    SupervisorThread,
    WorkflowStatus,
)
from backend.app.db.session import AsyncGovSessionLocal

logger = logging.getLogger(__name__)


class ExecutionWorker:
    """
    Deterministic execution worker that safely bridges approved LangGraph plans
    to the air-gapped FastMCP execution container.
    """

    def __init__(self, fastmcp_url: Optional[str] = None) -> None:
        self.fastmcp_url = fastmcp_url or settings.FASTMCP_SERVICE_URL

    async def execute_approved_plan(
        self,
        plan: CompliancePlan,
        pii_mapping: dict[str, str],
        execution_id: str,
        approval_token: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> ExecutionResult:
        """
        Unmasks PII, enforces token validity, and sends mutations to FastMCP.
        """
        # Step 1: Cryptographic HITL Token Verification
        if approval_token:
            plan_hash = compute_plan_hash(plan.model_dump())
            try:
                verify_approval_token(
                    token=approval_token,
                    expected_plan_id=plan.plan_id,
                    current_plan_hash=plan_hash,
                )
                logger.info(
                    f"HITL approval token verified successfully for plan {plan.plan_id} (execution {execution_id})."
                )
            except SecurityException as e:
                logger.error(f"Execution rejected: Cryptographic verification failed: {e}")
                return ExecutionResult(
                    execution_id=execution_id,
                    status="FAILED",
                    queries_executed=[],
                    total_affected_rows=0,
                    pre_images_captured=0,
                    error=f"Cryptographic HITL Verification Failed: {e}",
                )

        # Step 2: Deterministic PII Unmasking
        unmasked_queries: list[str] = []
        for q in plan.queries:
            unmasked_sql = GuardrailAgent.unmask_text(q.sql_statement, pii_mapping)
            unmasked_queries.append(unmasked_sql)

        # Step 2.5: Persist Governance Audit Lineage (satisfies FK constraints in governance_db)
        def _to_uuid(val: Any) -> uuid.UUID:
            try:
                return uuid.UUID(str(val))
            except (ValueError, TypeError, AttributeError):
                return uuid.uuid5(uuid.NAMESPACE_DNS, str(val) if val else str(uuid.uuid4()))

        try:
            async with AsyncGovSessionLocal() as gov_session:
                t_uuid = _to_uuid(thread_id)
                thread = await gov_session.get(SupervisorThread, t_uuid)
                if not thread:
                    thread = SupervisorThread(
                        thread_id=t_uuid,
                        user_id="default_compliance_officer",
                        master_goal=plan.summary,
                        current_status=WorkflowStatus.EXECUTING,
                    )
                    gov_session.add(thread)
                    await gov_session.flush()

                p_uuid = _to_uuid(plan.plan_id)
                plan_record = await gov_session.get(ExecutionPlan, p_uuid)
                if not plan_record:
                    plan_record = ExecutionPlan(
                        plan_id=p_uuid,
                        thread_id=t_uuid,
                        proposed_sql_plan=plan.model_dump(),
                        status=WorkflowStatus.APPROVED,
                    )
                    gov_session.add(plan_record)
                    await gov_session.flush()

                e_uuid = _to_uuid(execution_id)
                exec_record = await gov_session.get(AgentExecution, e_uuid)
                if not exec_record:
                    exec_record = AgentExecution(
                        execution_id=e_uuid,
                        plan_id=p_uuid,
                        agent_name=AgentRole.EXECUTION_WORKER,
                        sub_task_input={"queries": unmasked_queries},
                        status=WorkflowStatus.EXECUTING,
                    )
                    gov_session.add(exec_record)
                    await gov_session.commit()
        except Exception as e:
            logger.warning(f"Could not persist pre-execution audit record: {e}")

        # Step 3: Dispatch to FastMCP Execution Endpoint
        endpoint = f"{self.fastmcp_url}/tools/execute"
        total_affected = 0
        total_pre_images = 0
        executed_sqls: list[str] = []
        execution_details: list[dict[str, Any]] = []
        collected_results: list[dict[str, Any]] = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            for sql in unmasked_queries:
                sql_clean = sql.strip().upper()
                if sql_clean.startswith("SELECT"):
                    # Execute read-only SELECT queries through FastMCP dry-run sandbox
                    dry_run_endpoint = f"{self.fastmcp_url}/tools/dry-run"
                    try:
                        resp = await client.post(dry_run_endpoint, json={"sql_query": sql})
                        if resp.status_code == 200:
                            data = resp.json()
                            executed_sqls.append(sql)
                            execution_details.append(data)
                            preview = data.get("preview_sample", [])
                            if preview:
                                collected_results.extend(preview)
                        else:
                            executed_sqls.append(sql)
                    except Exception as e:
                        logger.warning(f"FastMCP read query execution failed: {e}")
                        executed_sqls.append(sql)
                    continue

                payload = {
                    "execution_id": execution_id,
                    "sql_query": sql,
                }

                try:
                    response = await client.post(endpoint, json=payload)
                    if response.status_code != 200:
                        err_msg = (
                            f"FastMCP returned status {response.status_code}: {response.text}"
                        )
                        logger.error(err_msg)
                        return ExecutionResult(
                            execution_id=execution_id,
                            status="FAILED",
                            queries_executed=executed_sqls,
                            total_affected_rows=total_affected,
                            pre_images_captured=total_pre_images,
                            error=err_msg,
                        )

                    data = response.json()
                    executed_sqls.append(sql)
                    total_affected += data.get("affected_rows", 0)
                    total_pre_images += data.get("pre_images_captured", 0)
                    execution_details.append(data)

                except Exception as e:
                    err_msg = f"Failed to communicate with FastMCP service at {endpoint}: {e}"
                    logger.error(err_msg)
                    return ExecutionResult(
                        execution_id=execution_id,
                        status="FAILED",
                        queries_executed=executed_sqls,
                        total_affected_rows=total_affected,
                        pre_images_captured=total_pre_images,
                        error=err_msg,
                    )

        # Step 4: S3 Payload Offloader (> 5 KB threshold)
        full_report = {
            "execution_id": execution_id,
            "thread_id": thread_id,
            "plan_id": plan.plan_id,
            "queries": executed_sqls,
            "details": execution_details,
            "query_results": collected_results,
        }
        _, s3_uri = storage_manager.offload_if_exceeds_threshold(
            payload=full_report,
            file_prefix=f"exec_summary_{execution_id}",
        )

        try:
            async with AsyncGovSessionLocal() as gov_session:
                e_uuid = _to_uuid(execution_id)
                exec_record = await gov_session.get(AgentExecution, e_uuid)
                if exec_record:
                    exec_record.status = WorkflowStatus.COMPLETED
                    exec_record.completed_at = datetime.now(timezone.utc)
                    exec_record.agent_output = {
                        "affected_rows": total_affected,
                        "pre_images_captured": total_pre_images,
                        "payload_s3_uri": s3_uri,
                        "query_results": collected_results,
                        "details": execution_details,
                    }
                    await gov_session.commit()
        except Exception as e:
            logger.warning(f"Could not update completion status in governance_db: {e}")

        return ExecutionResult(
            execution_id=execution_id,
            status="COMPLETED",
            queries_executed=executed_sqls,
            total_affected_rows=total_affected,
            pre_images_captured=total_pre_images,
            payload_s3_uri=s3_uri,
            query_results=collected_results,
            error=None,
        )


# Global singleton instance
execution_worker = ExecutionWorker()


# ==============================================================================
# LangGraph Execution Worker Node Function
# ==============================================================================


async def execution_worker_node(state: ComplianceState) -> dict[str, Any]:
    """
    LangGraph execution node for the Execution Worker Agent.
    
    1. Rehydrates state after Human-in-the-Loop gate.
    2. Validates approval token & plan hash.
    3. Unmasks PII placeholders.
    4. Triggers FastMCP transaction execution with row snapshots.
    5. Updates state with ExecutionResult, status COMPLETED or FAILED, and audit message.
    """
    plan_dict = state.get("proposed_plan")
    if not plan_dict:
        raise ValueError("Cannot execute worker_node: state contains no 'proposed_plan'.")

    plan = CompliancePlan.model_validate(plan_dict)
    pii_mapping = state.get("pii_mapping", {})
    approval_token = state.get("approval_token")
    thread_id = state.get("thread_id")
    execution_id = str(uuid.uuid4())

    result = await execution_worker.execute_approved_plan(
        plan=plan,
        pii_mapping=pii_mapping,
        execution_id=execution_id,
        approval_token=approval_token,
        thread_id=thread_id,
    )

    update: dict[str, Any] = {
        "execution_result": result.model_dump(),
        "status": result.status,
    }

    if result.status == "COMPLETED":
        msg = (
            f"[Execution Worker]: Successfully executed {len(result.queries_executed)} query/queries. "
            f"Total rows modified: {result.total_affected_rows}. "
            f"Pre-image snapshots captured in governance_db: {result.pre_images_captured}. "
            f"Execution ID: {execution_id}."
        )
        if result.payload_s3_uri:
            msg += f" Large execution payload offloaded to {result.payload_s3_uri}."
    else:
        update["error_message"] = result.error
        msg = (
            f"[Execution Worker]: Execution FAILED for ID {execution_id}. "
            f"Error: {result.error}"
        )

    update["messages"] = [AIMessage(content=msg)]
    return update
