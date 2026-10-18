"""
Unit tests for the Execution Worker Agent (Step 15).
Validates cryptographic HITL token verification, deterministic PII unmasking,
FastMCP transaction execution with row snapshots, MinIO offloading, and LangGraph node state integration.
"""

import uuid
import pytest

from backend.app.agents.execution_worker import ExecutionWorker, execution_worker_node
from backend.app.agents.state import CompliancePlan, ExecutionResult, MigrationQuery, create_initial_state
from backend.app.core.security import compute_plan_hash, generate_approval_token


@pytest.fixture
def sample_plan():
    return CompliancePlan(
        plan_id=str(uuid.uuid4()),
        summary="Normalize country code for test client",
        targeted_tables=["clients"],
        rationale="Unit test compliance execution",
        queries=[
            MigrationQuery(
                target_table="clients",
                operation="UPDATE",
                sql_statement="UPDATE clients SET country_code = 'DE' WHERE full_name = '[MASKED_PERSON_1]' AND is_deleted = FALSE;",
                rationale="Normalize German country code",
                is_safe=True,
            )
        ],
    )


@pytest.fixture
def sample_pii_mapping():
    return {
        "[MASKED_PERSON_1]": "Hans Gruber",
        "[MASKED_EMAIL_1]": "HANS.GRUBER@Acme-Gmbh.DE ",
    }


@pytest.mark.asyncio
async def test_cryptographic_token_rejection(sample_plan, sample_pii_mapping):
    """
    Asserts that an invalid, forged, or tampered approval token is rejected,
    failing the execution immediately.
    """
    worker = ExecutionWorker()
    invalid_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.forged.signature"

    result = await worker.execute_approved_plan(
        plan=sample_plan,
        pii_mapping=sample_pii_mapping,
        execution_id=str(uuid.uuid4()),
        approval_token=invalid_token,
    )

    assert isinstance(result, ExecutionResult)
    assert result.status == "FAILED"
    assert "Cryptographic HITL Verification Failed" in result.error
    assert result.total_affected_rows == 0


@pytest.mark.asyncio
async def test_execute_approved_plan_live_fastmcp(sample_plan, sample_pii_mapping):
    """
    Asserts that a validly signed plan unmasks PII tokens and successfully executes
    against the FastMCP microservice, capturing row pre-images.
    """
    worker = ExecutionWorker()
    thread_id = str(uuid.uuid4())
    execution_id = str(uuid.uuid4())

    # Generate valid cryptographic HITL token
    plan_hash = compute_plan_hash(sample_plan.model_dump())
    valid_token = generate_approval_token(
        thread_id=thread_id,
        plan_id=sample_plan.plan_id,
        plan_hash=plan_hash,
    )

    result = await worker.execute_approved_plan(
        plan=sample_plan,
        pii_mapping=sample_pii_mapping,
        execution_id=execution_id,
        approval_token=valid_token,
        thread_id=thread_id,
    )

    assert isinstance(result, ExecutionResult)
    assert result.status == "COMPLETED"
    assert result.error is None
    # Unmasked SQL must contain the real name, not the masked placeholder
    assert "Hans Gruber" in result.queries_executed[0]
    assert "[MASKED_PERSON_1]" not in result.queries_executed[0]
    # Hans Gruber exists in staging_db, so rows should be affected and pre-images saved
    assert result.total_affected_rows >= 1
    assert result.pre_images_captured >= 1


@pytest.mark.asyncio
async def test_execution_worker_langgraph_node(sample_plan, sample_pii_mapping):
    """
    Asserts that execution_worker_node properly transitions LangGraph state
    to COMPLETED and attaches audit messaging.
    """
    thread_id = str(uuid.uuid4())
    initial_state = create_initial_state(
        user_prompt="Normalize country code for Hans Gruber",
        thread_id=thread_id,
    )

    plan_hash = compute_plan_hash(sample_plan.model_dump())
    valid_token = generate_approval_token(
        thread_id=thread_id,
        plan_id=sample_plan.plan_id,
        plan_hash=plan_hash,
    )

    initial_state["proposed_plan"] = sample_plan.model_dump()
    initial_state["pii_mapping"] = sample_pii_mapping
    initial_state["approval_token"] = valid_token
    initial_state["status"] = "APPROVED"

    update = await execution_worker_node(initial_state)

    assert update["status"] == "COMPLETED"
    assert "execution_result" in update
    exec_res = update["execution_result"]
    assert exec_res["status"] == "COMPLETED"
    assert exec_res["total_affected_rows"] >= 1
    assert len(update["messages"]) == 1
    assert "Successfully executed" in update["messages"][0].content
