"""
Unit tests for Step 11: Agent State Definition & Schema (backend/app/agents/state.py).
"""

import json
import uuid
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages

from backend.app.agents.state import (
    CompliancePlan,
    ComplianceState,
    ExecutionResult,
    JudgeEvaluation,
    MigrationQuery,
    PIIEntity,
    create_initial_state,
)


def test_initial_state_creation():
    """Verify factory initializes ComplianceState with clean defaults."""
    prompt = "Standardize EU client country codes and billing addresses"
    state = create_initial_state(user_prompt=prompt, user_id="compliance_admin_01")

    assert state["original_prompt"] == prompt
    assert state["user_id"] == "compliance_admin_01"
    assert state["status"] == "PENDING_REASONING"
    assert state["replan_count"] == 0
    assert state["sanitized_prompt"] == ""
    assert state["pii_mapping"] == {}
    assert state["detected_pii"] == []
    assert state["schema_context"] == {}
    assert state["proposed_plan"] is None
    assert state["judge_evaluation"] is None
    assert state["execution_result"] is None
    assert len(state["messages"]) == 0
    # Assert thread_id is a valid UUID
    uuid.UUID(state["thread_id"])


def test_pydantic_sub_models_validation():
    """Verify validation and serialization of domain sub-models."""
    # 1. PIIEntity
    pii = PIIEntity(
        entity_type="EMAIL_ADDRESS",
        original_value="hans.gruber@nakatomi.de",
        token_mask="[MASKED_EMAIL_1]",
        start_pos=10,
        end_pos=34,
    )
    assert pii.token_mask == "[MASKED_EMAIL_1]"

    # 2. MigrationQuery
    query = MigrationQuery(
        target_table="clients",
        operation="UPDATE",
        sql_statement="UPDATE clients SET country_code = 'DE' WHERE country_code = 'Deutschland';",
        rationale="Standardize country to ISO-3166 alpha-2",
    )
    assert query.is_safe is True

    # 3. CompliancePlan
    plan = CompliancePlan(
        summary="Standardize German country codes",
        targeted_tables=["clients"],
        queries=[query],
        is_read_only=False,
        simulated_affected_rows=1,
        rationale="GDPR Article 5 data accuracy requirement",
    )
    assert len(plan.queries) == 1
    assert plan.is_read_only is False

    # 4. JudgeEvaluation
    judge = JudgeEvaluation(
        score=0.95,
        passed=True,
        verdict="VERIFIED_SAFE",
        feedback="All queries contain strict WHERE clauses with no destructive keywords.",
    )
    assert judge.passed is True

    # 5. ExecutionResult
    exec_res = ExecutionResult(
        execution_id=str(uuid.uuid4()),
        status="COMPLETED",
        queries_executed=[query.sql_statement],
        total_affected_rows=1,
        pre_images_captured=1,
    )
    assert exec_res.status == "COMPLETED"


def test_compliance_state_json_serializability():
    """Verify that a fully populated ComplianceState serializes to JSON seamlessly."""
    state = create_initial_state(user_prompt="Normalize customer phones and emails")

    pii = PIIEntity(
        entity_type="PHONE_NUMBER",
        original_value="+49 89 123456",
        token_mask="[MASKED_PHONE_1]",
        start_pos=0,
        end_pos=14,
    )
    state["sanitized_prompt"] = "Normalize customer [MASKED_PHONE_1] and emails"
    state["pii_mapping"] = {"[MASKED_PHONE_1]": "+49 89 123456"}
    state["detected_pii"] = [pii.model_dump()]

    plan = CompliancePlan(
        summary="Phone normalization",
        targeted_tables=["clients"],
        queries=[
            MigrationQuery(
                target_table="clients",
                operation="UPDATE",
                sql_statement="UPDATE clients SET phone = '+4989123456' WHERE id = 'a1111111-1111-1111-1111-111111111111';",
                rationale="Strip non-numeric characters",
            )
        ],
        rationale="Consistent E.164 phone format",
    )
    state["proposed_plan"] = plan.model_dump()

    judge = JudgeEvaluation(
        score=0.92,
        passed=True,
        verdict="VERIFIED_SAFE",
        feedback="Scattering scoped strictly to single primary key.",
    )
    state["judge_evaluation"] = judge.model_dump()

    state["messages"] = [
        HumanMessage(content=state["original_prompt"]),
        AIMessage(content="Generated normalization plan."),
    ]

    # Convert state to dict and ensure JSON dumps succeeds
    state_dict = dict(state)
    # Convert messages to serializable format for JSON
    serializable_dict = {
        k: [m.model_dump() if hasattr(m, "model_dump") else m for m in v]
        if k == "messages"
        else v
        for k, v in state_dict.items()
    }
    json_str = json.dumps(serializable_dict)
    assert len(json_str) > 0
    assert "[MASKED_PHONE_1]" in json_str


def test_langgraph_message_reducer():
    """Verify add_messages reducer appends messages without mutation."""
    current_msgs = [HumanMessage(content="Hello AI Agent")]
    new_msgs = [AIMessage(content="Hello! Ready to verify compliance.")]

    combined = add_messages(current_msgs, new_msgs)
    assert len(combined) == 2
    assert combined[0].content == "Hello AI Agent"
    assert combined[1].content == "Hello! Ready to verify compliance."
