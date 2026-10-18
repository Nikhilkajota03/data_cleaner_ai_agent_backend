"""
Unit tests for the Safety Judge Agent (Step 14).
Validates deterministic security pre-checks (missing WHERE, WHERE 1=1, DROP, hard DELETE),
LLM-as-a-Judge semantic scoring, LangGraph node state updates, and reflection loop routing.
"""

import pytest

from backend.app.agents.judge import SafetyJudge, judge_node
from backend.app.agents.state import (
    CompliancePlan,
    JudgeEvaluation,
    MigrationQuery,
    create_initial_state,
)


@pytest.fixture
def sample_schema_context():
    return {
        "tables": {
            "clients": [
                {"column_name": "id", "data_type": "uuid"},
                {"column_name": "full_name", "data_type": "varchar"},
                {"column_name": "country_code", "data_type": "varchar"},
                {"column_name": "is_deleted", "data_type": "boolean"},
            ]
        }
    }


def test_deterministic_precheck_missing_where():
    judge = SafetyJudge()
    unsafe_plan = CompliancePlan(
        summary="Unsafe bulk update",
        targeted_tables=["clients"],
        rationale="Testing missing WHERE clause rejection",
        queries=[
            MigrationQuery(
                target_table="clients",
                operation="UPDATE",
                sql_statement="UPDATE clients SET status = 'ACTIVE';",
                rationale="Bulk update without filter",
                is_safe=False,
            )
        ],
    )

    violations = judge.run_deterministic_prechecks(unsafe_plan)
    assert len(violations) >= 1
    assert any("lacks a WHERE clause" in v for v in violations)


def test_deterministic_precheck_tautology():
    judge = SafetyJudge()
    unsafe_plan = CompliancePlan(
        summary="Unsafe tautological update",
        targeted_tables=["clients"],
        rationale="Testing 1=1 rejection",
        queries=[
            MigrationQuery(
                target_table="clients",
                operation="UPDATE",
                sql_statement="UPDATE clients SET status = 'ACTIVE' WHERE 1=1;",
                rationale="Tautological filter",
                is_safe=False,
            )
        ],
    )

    violations = judge.run_deterministic_prechecks(unsafe_plan)
    assert len(violations) >= 1
    assert any("tautological 'WHERE 1=1'" in v for v in violations)


def test_deterministic_precheck_destructive_ddl():
    judge = SafetyJudge()
    unsafe_plan = CompliancePlan(
        summary="Malicious drop table",
        targeted_tables=["clients"],
        rationale="Testing DROP TABLE rejection",
        queries=[
            MigrationQuery(
                target_table="clients",
                operation="DROP",
                sql_statement="DROP TABLE clients;",
                rationale="Malicious operation",
                is_safe=False,
            )
        ],
    )

    violations = judge.run_deterministic_prechecks(unsafe_plan)
    assert len(violations) >= 1
    assert any("DROP TABLE" in v for v in violations)


def test_deterministic_precheck_hard_delete():
    judge = SafetyJudge()
    unsafe_plan = CompliancePlan(
        summary="Hard delete violation",
        targeted_tables=["clients"],
        rationale="Testing hard delete rejection",
        queries=[
            MigrationQuery(
                target_table="clients",
                operation="DELETE",
                sql_statement="DELETE FROM clients WHERE id = 'e5b14f82-72c1-4b11-9755-f2d1e2e18b01';",
                rationale="Hard delete instead of soft delete",
                is_safe=False,
            )
        ],
    )

    violations = judge.run_deterministic_prechecks(unsafe_plan)
    assert len(violations) >= 1
    assert any("hard DELETE statement detected" in v for v in violations)


@pytest.mark.asyncio
async def test_evaluate_unsafe_plan_short_circuits(sample_schema_context):
    judge = SafetyJudge()
    unsafe_plan = CompliancePlan(
        summary="Critical unsafe plan",
        targeted_tables=["clients"],
        rationale="Testing short-circuit on deterministic failure",
        queries=[
            MigrationQuery(
                target_table="clients",
                operation="UPDATE",
                sql_statement="UPDATE clients SET is_deleted = TRUE;",
                rationale="Missing WHERE",
                is_safe=False,
            )
        ],
    )

    eval_result = await judge.evaluate_plan(
        plan=unsafe_plan,
        sanitized_prompt="Delete all clients",
        schema_context=sample_schema_context,
    )

    assert isinstance(eval_result, JudgeEvaluation)
    assert eval_result.passed is False
    assert eval_result.verdict == "REJECTED_UNSAFE"
    assert eval_result.score <= 0.20
    assert len(eval_result.detected_violations) >= 1


@pytest.mark.asyncio
async def test_evaluate_safe_plan_with_llm(sample_schema_context):
    judge = SafetyJudge()
    safe_plan = CompliancePlan(
        summary="Normalize German country codes to ISO-2 'DE'",
        targeted_tables=["clients"],
        rationale="Clean dirty country names adhering to enterprise ISO standards",
        simulated_affected_rows=3,
        queries=[
            MigrationQuery(
                target_table="clients",
                operation="UPDATE",
                sql_statement=(
                    "UPDATE clients SET country_code = 'DE', updated_at = CURRENT_TIMESTAMP "
                    "WHERE country_code IN ('Deutschland', 'germany') AND is_deleted = FALSE;"
                ),
                rationale="Tightly scoped update to dirty German records only",
                is_safe=True,
            )
        ],
    )

    eval_result = await judge.evaluate_plan(
        plan=safe_plan,
        sanitized_prompt="Clean German country codes to DE",
        schema_context=sample_schema_context,
    )

    assert isinstance(eval_result, JudgeEvaluation)
    assert eval_result.passed is True
    assert eval_result.score >= 0.85
    assert eval_result.verdict == "VERIFIED_SAFE"


@pytest.mark.asyncio
async def test_judge_langgraph_node_approved():
    initial_state = create_initial_state(user_prompt="Normalize German country codes")
    initial_state["sanitized_prompt"] = initial_state["original_prompt"]
    initial_state["proposed_plan"] = {
        "plan_id": "test-plan-1",
        "summary": "Normalize German country codes",
        "targeted_tables": ["clients"],
        "is_read_only": False,
        "simulated_affected_rows": 2,
        "rationale": "Enterprise standard compliance",
        "queries": [
            {
                "target_table": "clients",
                "operation": "UPDATE",
                "sql_statement": "UPDATE clients SET country_code = 'DE' WHERE country_code = 'germany' AND is_deleted = FALSE;",
                "rationale": "Target dirty rows",
                "is_safe": True,
            }
        ],
    }

    update = await judge_node(initial_state)

    assert "judge_evaluation" in update
    assert update["status"] == "PENDING_APPROVAL"
    assert update["judge_evaluation"]["passed"] is True
    assert len(update["messages"]) == 1


@pytest.mark.asyncio
async def test_judge_langgraph_node_reflection_routing():
    initial_state = create_initial_state(user_prompt="Clean bad data")
    initial_state["replan_count"] = 0
    # Provide an unsafe plan
    initial_state["proposed_plan"] = {
        "plan_id": "test-plan-unsafe",
        "summary": "Unsafe wipe",
        "targeted_tables": ["clients"],
        "is_read_only": False,
        "simulated_affected_rows": 100,
        "rationale": "No WHERE clause",
        "queries": [
            {
                "target_table": "clients",
                "operation": "UPDATE",
                "sql_statement": "UPDATE clients SET status = 'INACTIVE';",
                "rationale": "Bulk wipe",
                "is_safe": False,
            }
        ],
    }

    update = await judge_node(initial_state)

    # First attempt: replan_count goes from 0 -> 1, status set to PENDING_REASONING
    assert update["status"] == "PENDING_REASONING"
    assert update["replan_count"] == 1
    assert update["judge_evaluation"]["passed"] is False


@pytest.mark.asyncio
async def test_judge_langgraph_node_max_retries_exceeded():
    initial_state = create_initial_state(user_prompt="Clean bad data")
    initial_state["replan_count"] = 2  # Already tried 2 times
    # Provide another unsafe plan
    initial_state["proposed_plan"] = {
        "plan_id": "test-plan-unsafe-3",
        "summary": "Third unsafe attempt",
        "targeted_tables": ["clients"],
        "is_read_only": False,
        "simulated_affected_rows": 100,
        "rationale": "DROP TABLE attempt",
        "queries": [
            {
                "target_table": "clients",
                "operation": "DROP",
                "sql_statement": "DROP TABLE clients;",
                "rationale": "Destructive query",
                "is_safe": False,
            }
        ],
    }

    update = await judge_node(initial_state)

    # Third failure exceeds threshold: status set to FAILED
    assert update["status"] == "FAILED"
    assert update["replan_count"] == 3
    assert "error_message" in update
    assert "permanently rejected" in update["error_message"].lower()
