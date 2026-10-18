"""
Unit tests for the Compliance Planner Agent (Step 13).
Validates schema context introspection, Groq-driven SQL migration synthesis,
mandatory WHERE clause enforcement, PII token preservation, and reflection loops.
"""

import pytest

from backend.app.agents.planner import CompliancePlanner, planner_node
from backend.app.agents.state import CompliancePlan, create_initial_state


@pytest.mark.asyncio
async def test_fetch_schema_context():
    """
    Assert that the planner can retrieve database schema context either
    from the live FastMCP container or the high-fidelity offline fallback.
    """
    planner = CompliancePlanner()
    schema = await planner.fetch_schema_context()

    assert "tables" in schema
    assert "clients" in schema["tables"]
    assert "invoices" in schema["tables"]

    # Verify column structure
    client_cols = [c["column_name"] for c in schema["tables"]["clients"]]
    assert "id" in client_cols
    assert "country_code" in client_cols
    assert "is_deleted" in client_cols


@pytest.mark.asyncio
async def test_generate_compliance_plan():
    """
    Assert that the planner generates a valid, deterministic CompliancePlan
    with mandatory non-trivial WHERE clauses.
    """
    planner = CompliancePlanner()
    schema = await planner.fetch_schema_context()

    prompt = (
        "Standardize all dirty German country names ('Deutschland', 'germany', 'DEU') "
        "in the clients table to the official ISO-2 code 'DE'."
    )

    plan = await planner.generate_plan(
        sanitized_prompt=prompt,
        schema_context=schema,
    )

    assert isinstance(plan, CompliancePlan)
    assert len(plan.queries) >= 1
    assert "clients" in plan.targeted_tables
    assert plan.simulated_affected_rows >= 1

    # Check each generated query for enterprise safety rules
    assert any(q.operation == "UPDATE" for q in plan.queries)
    for q in plan.queries:
        assert q.target_table == "clients"
        assert q.operation in ("UPDATE", "SELECT")
        sql_upper = q.sql_statement.upper()
        assert ("UPDATE" in sql_upper) or ("SELECT" in sql_upper)
        assert "WHERE" in sql_upper
        assert "DROP" not in sql_upper
        assert "TRUNCATE" not in sql_upper
        assert "1=1" not in sql_upper


@pytest.mark.asyncio
async def test_planner_preserves_masked_pii_tokens():
    """
    Assert that masked tokens (e.g. [MASKED_EMAIL_ADDRESS_1]) emitted by the Guardrail
    are preserved verbatim in the generated SQL queries.
    """
    planner = CompliancePlanner()
    schema = await planner.fetch_schema_context()

    prompt = (
        "Client [MASKED_PERSON_1] with email [MASKED_EMAIL_ADDRESS_1] requested GDPR erasure. "
        "Perform a compliant soft-delete on their account."
    )

    plan = await planner.generate_plan(
        sanitized_prompt=prompt,
        schema_context=schema,
    )

    assert isinstance(plan, CompliancePlan)
    combined_sql = " ".join(q.sql_statement for q in plan.queries)

    # Must preserve the exact masked token for deterministic execution worker unmasking
    assert "[MASKED_EMAIL_ADDRESS_1]" in combined_sql or "[MASKED_PERSON_1]" in combined_sql
    # Must enforce soft delete rather than hard DROP / physical DELETE
    assert "is_deleted" in combined_sql.lower()


@pytest.mark.asyncio
async def test_planner_reflection_feedback_adaptation():
    """
    Assert that when reflection feedback from the Safety Judge is provided,
    the planner takes the critique into account.
    """
    planner = CompliancePlanner()
    schema = await planner.fetch_schema_context()

    prompt = "Set billing status to active for clients."
    critique = (
        "CRITICAL SAFETY VIOLATION: Your previous query lacked a WHERE clause and attempted "
        "to update all 10,000 clients. You must restrict the query to only clients where status = 'PENDING'."
    )

    plan = await planner.generate_plan(
        sanitized_prompt=prompt,
        schema_context=schema,
        feedback_history=critique,
    )

    assert isinstance(plan, CompliancePlan)
    for q in plan.queries:
        assert "WHERE" in q.sql_statement.upper()
        # Should incorporate status filtering
        assert "status" in q.sql_statement.lower() or "pending" in q.sql_statement.lower()


@pytest.mark.asyncio
async def test_planner_langgraph_node():
    """
    Assert that the planner_node executes cleanly within the LangGraph state convention.
    """
    initial_state = create_initial_state(
        user_prompt="Normalize invalid Austrian postal codes to 4 digits for client with ID e5b14f82-72c1-4b11-9755-f2d1e2e18b01"
    )

    # Simulate output from previous guardrail node
    initial_state["sanitized_prompt"] = initial_state["original_prompt"]

    update = await planner_node(initial_state)

    assert "proposed_plan" in update
    assert "schema_context" in update
    assert "messages" in update

    plan_dict = update["proposed_plan"]
    assert "summary" in plan_dict
    assert "queries" in plan_dict
    assert len(plan_dict["queries"]) >= 1
    assert len(update["messages"]) == 1
