"""
Comprehensive Integration Test Suite for Phase 5 FastAPI Gateway & Control APIs.

Tests:
1. Health & Root Endpoints (GET /, GET /health)
2. Admin Seed, Data Retention Purge, & GDPR Hard-Delete
3. Staging CRM Introspection (Clients, Invoices, Pagination, Search)
4. Cryptographic HITL Approval, Token Verification, Tampering Defense, & Rejection
5. Rollback Endpoint Handling
6. Pipeline Execution & SSE Event Streaming
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import uuid
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.app.agents.graph import compliance_graph
from backend.app.agents.state import ComplianceState
from backend.app.core.config import settings
from backend.app.core.security import compute_plan_hash, generate_approval_token
from backend.app.db.models import (
    AgentExecution,
    AgentRole,
    Client,
    ExecutionPlan,
    Invoice,
    RowPreImage,
    SupervisorThread,
    WorkflowStatus,
)
from backend.app.db.session import AsyncGovSessionLocal, AsyncStagingSessionLocal
from backend.app.main import app


@pytest_asyncio.fixture
async def async_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# ==============================================================================
# 1. Health & Root Endpoint Tests (Step 17)
# ==============================================================================


@pytest.mark.asyncio
async def test_root_endpoint(async_client: AsyncClient):
    response = await async_client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == settings.APP_NAME
    assert data["health_check"] == "/health"
    assert data["api_v1_prefix"] == "/api"


@pytest.mark.asyncio
async def test_health_check_endpoint(async_client: AsyncClient):
    response = await async_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("HEALTHY", "DEGRADED")
    assert data["postgres_governance_db"] == "HEALTHY"
    assert data["postgres_staging_db"] == "HEALTHY"
    assert data["redis"] == "HEALTHY"


# ==============================================================================
# 2. Admin & Retention Tests (Step 22)
# ==============================================================================


@pytest.mark.asyncio
async def test_seed_demo_data(async_client: AsyncClient):
    response = await async_client.post("/api/admin/seed-demo-data")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "SUCCESS"
    assert data["clients_seeded"] >= 3
    assert len(data["sample_client_ids"]) >= 3


@pytest.mark.asyncio
async def test_retention_purge_endpoint(async_client: AsyncClient):
    # Ensure there is an expired soft-deleted record in staging_db
    expired_client_id = uuid.uuid4()
    past_date = datetime.now(timezone.utc) - timedelta(days=40)

    async with AsyncStagingSessionLocal() as session:
        exp_client = Client(
            id=expired_client_id,
            full_name="Obsolete Purge Candidate",
            email="obsolete@test.org",
            gdpr_consent=False,
            status="INACTIVE",
            is_deleted=True,
            deleted_at=past_date,
        )
        session.add(exp_client)
        await session.commit()

    # Trigger retention purge
    response = await async_client.post("/api/admin/purge", json={"retention_days": 30})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "SUCCESS"
    assert data["purged_clients"] >= 1

    # Verify physical deletion
    async with AsyncStagingSessionLocal() as session:
        check = await session.execute(select(Client).where(Client.id == expired_client_id))
        assert check.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_gdpr_hard_delete_endpoint(async_client: AsyncClient):
    target_id = uuid.uuid4()
    async with AsyncStagingSessionLocal() as session:
        client_to_erase = Client(
            id=target_id,
            full_name="GDPR Right to Erasure Subject",
            email="erasure@gdpr.de",
            gdpr_consent=False,
            status="ACTIVE",
            is_deleted=False,
        )
        inv = Invoice(
            id=uuid.uuid4(),
            client_id=target_id,
            invoice_number="INV-ERASE-001",
            amount_standardized=500.0,
            currency="EUR",
            invoice_date=date(2025, 1, 1),
            is_deleted=False,
        )
        session.add_all([client_to_erase, inv])
        await session.commit()

    # Call hard-delete endpoint
    resp = await async_client.delete(f"/api/admin/clients/{target_id}/hard-delete")
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["status"] == "SUCCESS"
    assert res_data["purged_clients"] == 1
    assert res_data["purged_invoices"] == 1

    # Verify client is gone from DB
    async with AsyncStagingSessionLocal() as session:
        check = await session.execute(select(Client).where(Client.id == target_id))
        assert check.scalar_one_or_none() is None


# ==============================================================================
# 3. CRM Introspection Tests (Step 21)
# ==============================================================================


@pytest.mark.asyncio
async def test_crm_introspection_clients(async_client: AsyncClient):
    # List clients
    resp = await async_client.get("/api/crm/clients?limit=10")
    assert resp.status_code == 200
    clients = resp.json()
    assert isinstance(clients, list)
    assert len(clients) > 0

    first_client = clients[0]
    client_id = first_client["id"]

    # Detail view
    detail_resp = await async_client.get(f"/api/crm/clients/{client_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["id"] == client_id
    assert "invoices" in detail


@pytest.mark.asyncio
async def test_crm_introspection_invoices(async_client: AsyncClient):
    resp = await async_client.get("/api/crm/invoices?limit=10")
    assert resp.status_code == 200
    invoices = resp.json()
    assert isinstance(invoices, list)
    assert len(invoices) > 0


# ==============================================================================
# 4. Cryptographic HITL Approval & Rejection Tests (Step 19)
# ==============================================================================


@pytest.mark.asyncio
async def test_get_pending_plan_and_token(async_client: AsyncClient):
    thread_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())

    dummy_plan = {
        "plan_id": plan_id,
        "summary": "Standardize phone numbers for German clients",
        "targeted_tables": ["clients"],
        "queries": [
            {
                "target_table": "clients",
                "operation": "UPDATE",
                "sql_statement": "UPDATE clients SET phone = '+49' || substring(phone from 2) WHERE phone LIKE '0%'",
                "rationale": "E.164 normalization",
                "is_safe": True,
            }
        ],
        "is_read_only": False,
        "simulated_affected_rows": 1,
        "rationale": "GDPR and standardization",
    }

    # Initialize state in compliance_graph with config
    config = {"configurable": {"thread_id": thread_id}}
    initial_state = {
        "thread_id": thread_id,
        "user_id": "officer_test",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "original_prompt": "Standardize german phones",
        "sanitized_prompt": "Standardize german phones",
        "pii_mapping": {},
        "detected_pii": [],
        "schema_context": {},
        "proposed_plan": dummy_plan,
        "judge_evaluation": {"score": 0.95, "passed": True, "verdict": "VERIFIED_SAFE", "feedback": "Safe"},
        "replan_count": 0,
        "status": "PENDING_APPROVAL",
        "approval_token": None,
        "approved_by": None,
        "approved_at": None,
        "execution_result": None,
        "error_message": None,
        "messages": [],
    }
    await compliance_graph.aupdate_state(config, initial_state)

    # Persist in DB
    async with AsyncGovSessionLocal() as session:
        st = SupervisorThread(
            thread_id=uuid.UUID(thread_id),
            user_id="officer_test",
            master_goal="Standardize german phones",
            current_status=WorkflowStatus.PENDING_APPROVAL,
        )
        ep = ExecutionPlan(
            plan_id=uuid.UUID(plan_id),
            thread_id=uuid.UUID(thread_id),
            proposed_sql_plan=dummy_plan,
            judge_score=0.95,
            judge_feedback="Safe",
            status=WorkflowStatus.PENDING_APPROVAL,
        )
        session.add_all([st, ep])
        await session.commit()

    # Query pending endpoint
    resp = await async_client.get(f"/api/pipeline/{thread_id}/pending")
    assert resp.status_code == 200
    data = resp.json()
    assert data["thread_id"] == thread_id
    assert data["plan_id"] == plan_id
    assert "approval_token" in data
    assert data["plan_hash"] == compute_plan_hash(dummy_plan)


@pytest.mark.asyncio
async def test_approve_plan_tampered_token_rejected(async_client: AsyncClient):
    thread_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())

    plan_original = {
        "plan_id": plan_id,
        "summary": "Original Plan",
        "queries": [],
    }
    plan_hash_original = compute_plan_hash(plan_original)
    valid_token = generate_approval_token(thread_id, plan_id, plan_hash_original)

    # But in graph, the plan has been modified/tampered!
    tampered_plan = {
        "plan_id": plan_id,
        "summary": "Malicious Tampered Plan",
        "queries": [{"target_table": "clients", "operation": "DELETE", "sql_statement": "DELETE FROM clients WHERE 1=1"}],
    }

    config = {"configurable": {"thread_id": thread_id}}
    await compliance_graph.aupdate_state(
        config,
        {
            "thread_id": thread_id,
            "status": "PENDING_APPROVAL",
            "proposed_plan": tampered_plan,
        },
    )

    # Submit approval with token bound to original plan
    resp = await async_client.post(
        "/api/pipeline/approve",
        json={
            "thread_id": thread_id,
            "approval_token": valid_token,
            "approver_id": "officer_test",
        },
    )
    # Must fail with 403 Forbidden due to hash mismatch
    assert resp.status_code == 403
    assert "does not match the hash" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_reject_plan_endpoint(async_client: AsyncClient):
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    # Setup state
    await compliance_graph.aupdate_state(
        config,
        {
            "thread_id": thread_id,
            "status": "PENDING_APPROVAL",
            "proposed_plan": {"plan_id": str(uuid.uuid4()), "summary": "Unwanted Plan"},
        },
    )

    async with AsyncGovSessionLocal() as session:
        st = SupervisorThread(
            thread_id=uuid.UUID(thread_id),
            user_id="officer_test",
            master_goal="Goal to reject",
            current_status=WorkflowStatus.PENDING_APPROVAL,
        )
        session.add(st)
        await session.commit()

    resp = await async_client.post(
        "/api/pipeline/reject",
        json={
            "thread_id": thread_id,
            "rejection_reason": "Incompatible with current client data policy",
            "rejected_by": "compliance_lead",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "REJECTED"
    assert "Incompatible" in data["rejection_reason"]


# ==============================================================================
# 5. Rollback Endpoint Tests (Step 20)
# ==============================================================================


@pytest.mark.asyncio
async def test_rollback_unknown_execution_returns_404(async_client: AsyncClient):
    fake_id = str(uuid.uuid4())
    resp = await async_client.post(f"/api/pipeline/{fake_id}/rollback")
    assert resp.status_code == 404
    assert f"'{fake_id}' does not exist" in resp.json()["detail"]


# ==============================================================================
# 6. Pipeline Run & SSE Stream Tests (Step 18)
# ==============================================================================


@pytest.mark.asyncio
async def test_pipeline_run_background_mode(async_client: AsyncClient):
    from unittest.mock import patch
    prompt = "Verify phone format for client Hans Gruber in Nakatomi Plaza"
    with patch("backend.app.api.pipeline._execute_pipeline_task"):
        resp = await async_client.post(
            "/api/pipeline/run",
            json={
                "user_prompt": prompt,
                "user_id": "test_agent_user",
                "wait_for_pause": False,
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert "thread_id" in data
        assert data["status"] == "PENDING_REASONING"


@pytest.mark.asyncio
async def test_pipeline_sse_stream_endpoint(async_client: AsyncClient):
    thread_id = str(uuid.uuid4())
    # Connect to stream with a short timeout
    resp = await async_client.get(f"/api/pipeline/{thread_id}/stream?timeout=5.0")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


# ==============================================================================
# 7. End-to-End Approval, Resumption & Rollback Verification
# ==============================================================================


@pytest.mark.asyncio
async def test_approve_and_execute_with_rollback(async_client: AsyncClient):
    # 1. Seed a specific client in staging_db with dirty phone
    target_client_id = uuid.uuid4()
    original_phone = "089-998877"

    async with AsyncStagingSessionLocal() as session:
        test_client = Client(
            id=target_client_id,
            full_name="Rollback Target Subject",
            email="rollback.subject@corp.de",
            phone=original_phone,
            gdpr_consent=True,
            status="ACTIVE",
            is_deleted=False,
        )
        session.add(test_client)
        await session.commit()

    # 2. Build migration plan updating phone to +4989998877
    thread_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())
    clean_phone = "+4989998877"

    plan = {
        "plan_id": plan_id,
        "summary": "Normalize phone format for Rollback Target Subject",
        "targeted_tables": ["clients"],
        "queries": [
            {
                "target_table": "clients",
                "operation": "UPDATE",
                "sql_statement": f"UPDATE clients SET phone = '{clean_phone}' WHERE id = '{target_client_id}'",
                "rationale": "E.164 standardization",
                "is_safe": True,
            }
        ],
        "is_read_only": False,
        "simulated_affected_rows": 1,
        "rationale": "Testing HITL approval and FastMCP pre-image capture",
    }

    plan_hash = compute_plan_hash(plan)
    valid_token = generate_approval_token(thread_id, plan_id, plan_hash)

    # 3. Setup thread and plan in governance_db
    t_uuid = uuid.UUID(thread_id)
    p_uuid = uuid.UUID(plan_id)

    async with AsyncGovSessionLocal() as session:
        st = SupervisorThread(
            thread_id=t_uuid,
            user_id="compliance_lead",
            master_goal="Normalize phone format",
            current_status=WorkflowStatus.PENDING_APPROVAL,
        )
        ep = ExecutionPlan(
            plan_id=p_uuid,
            thread_id=t_uuid,
            proposed_sql_plan=plan,
            judge_score=0.98,
            judge_feedback="Verified safe",
            approval_token_hash=plan_hash,
            status=WorkflowStatus.PENDING_APPROVAL,
        )
        session.add_all([st, ep])
        await session.commit()

    # 4. Setup paused state in LangGraph checkpointer
    config = {"configurable": {"thread_id": thread_id}}
    paused_state = {
        "thread_id": thread_id,
        "user_id": "compliance_lead",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "original_prompt": "Normalize phone format",
        "sanitized_prompt": "Normalize phone format",
        "pii_mapping": {},
        "detected_pii": [],
        "schema_context": {},
        "proposed_plan": plan,
        "judge_evaluation": {"score": 0.98, "passed": True, "verdict": "VERIFIED_SAFE", "feedback": "Safe"},
        "replan_count": 0,
        "status": "PENDING_APPROVAL",
        "approval_token": None,
        "approved_by": None,
        "approved_at": None,
        "execution_result": None,
        "error_message": None,
        "messages": [],
    }
    await compliance_graph.aupdate_state(config, paused_state, as_node="judge")

    # 5. Call POST /api/pipeline/approve to resume graph & execute
    approve_resp = await async_client.post(
        "/api/pipeline/approve",
        json={
            "thread_id": thread_id,
            "approval_token": valid_token,
            "approver_id": "chief_compliance_officer",
        },
    )
    assert approve_resp.status_code == 200
    approve_data = approve_resp.json()
    assert approve_data["status"] == "COMPLETED"
    assert approve_data["total_affected_rows"] == 1
    assert approve_data["pre_images_captured"] == 1
    execution_id = approve_data["execution_id"]
    assert execution_id is not None

    # 6. Verify row in staging_db was updated
    async with AsyncStagingSessionLocal() as session:
        check = await session.execute(select(Client).where(Client.id == target_client_id))
        updated_client = check.scalar_one()
        assert updated_client.phone == clean_phone

    # 7. Trigger Rollback via POST /api/pipeline/{execution_id}/rollback
    rollback_resp = await async_client.post(
        f"/api/pipeline/{execution_id}/rollback",
        json={"reason": "Testing 1-click rollback restoration"},
    )
    assert rollback_resp.status_code == 200
    rb_data = rollback_resp.json()
    assert rb_data["status"] == "ROLLED_BACK"
    assert rb_data["restored_rows"] == 1

    # 8. Verify row in staging_db reverted back to original dirty value
    async with AsyncStagingSessionLocal() as session:
        check = await session.execute(select(Client).where(Client.id == target_client_id))
        restored_client = check.scalar_one()
        assert restored_client.phone == original_phone
