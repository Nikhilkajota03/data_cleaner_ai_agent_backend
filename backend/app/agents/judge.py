"""
Safety Judge Agent: LLM-as-a-Judge for Automated SQL Migration Safety Evaluation.

This agent acts as an independent adversarial auditor:
1. Performs deterministic AST / heuristic pre-checks for unconstrained mutations and destructive DDL.
2. Prompts Groq LLM (qwen/qwen3.8-27b) in JSON Schema mode to evaluate alignment with user intent,
   data loss risks, and enterprise compliance rules.
3. Scores the plan on a 0.0 - 1.0 scale (threshold >= 0.85).
4. Emits a structured JudgeEvaluation driving either HITL human approval or an iterative reflection loop.
"""

import json
import logging
import re
from typing import Any, Optional

from langchain_core.messages import AIMessage
from langchain_groq import ChatGroq

from backend.app.agents.state import CompliancePlan, ComplianceState, JudgeEvaluation
from backend.app.core.config import settings

logger = logging.getLogger(__name__)


# ==============================================================================
# Judge Prompt & Evaluation Rubric
# ==============================================================================

JUDGE_SYSTEM_PROMPT = """You are the Principal Database Safety Officer and Lead Security Auditor for an Enterprise CRM.
Your objective is to independently audit a proposed database CompliancePlan against strict enterprise security, regulatory compliance, and safety standards.

ENTERPRISE AUDIT RUBRIC:
1. ZERO UNCONSTRAINED MUTATIONS (Weight: Critical):
   - Every UPDATE must possess an explicit, non-trivial WHERE clause.
   - WHERE 1=1 or tautologies are strictly forbidden and count as critical failure.
2. ZERO DESTRUCTIVE DDL (Weight: Critical):
   - DROP TABLE, TRUNCATE, and ALTER TABLE statements are completely banned.
3. SOFT-DELETE MANDATE (Weight: Critical):
   - Any record deletion must be implemented as a soft delete (`is_deleted = TRUE, deleted_at = CURRENT_TIMESTAMP`).
   - Physical hard deletes (`DELETE FROM ...`) are forbidden for AI agents.
4. TARGETING & SCOPE FIDELITY:
   - Does the SQL specifically address the user's intent without modifying unrelated columns or rows?
5. PRESERVATION OF PII TOKENS:
   - If the prompt referenced masked tokens like [MASKED_EMAIL_ADDRESS_1], are they preserved verbatim in the queries?

SCORING CRITERIA:
- 0.90 - 1.00: Flawless plan. Completely safe, tightly scoped WHERE clauses, perfect soft-delete compliance.
- 0.85 - 0.89: Safe with minor non-blocking recommendations.
- 0.50 - 0.84: REVISION REQUIRED. Ambiguous scoping, excessive rows targeted, or sub-optimal SQL.
- 0.00 - 0.49: REJECTED UNSAFE. Unconstrained mutation (missing WHERE), destructive DDL, or severe data loss risk.

YOU MUST OUTPUT VALID JSON ONLY conforming to the following JSON Schema:
{schema_json}
"""


class SafetyJudge:
    """
    Evaluates proposed CompliancePlans using hybrid deterministic AST checks
    and LLM-as-a-Judge semantic reasoning.
    """

    def __init__(self, model_name: Optional[str] = None) -> None:
        self.model_name = model_name or settings.GROQ_PLANNER_MODEL

    def _get_llm(self) -> ChatGroq:
        return ChatGroq(
            api_key=settings.GROQ_API_KEY,
            model=self.model_name,
            temperature=0.0,
            model_kwargs={"response_format": {"type": "json_object"}},
        )

    def run_deterministic_prechecks(self, plan: CompliancePlan) -> list[str]:
        """
        Fast heuristic / regex pre-checks to catch obvious critical security violations
        before invoking LLM inference.
        """
        violations: list[str] = []

        for idx, q in enumerate(plan.queries, start=1):
            sql_clean = q.sql_statement.strip().upper()

            # 1. Banned destructive keywords
            for banned in ("DROP TABLE", "DROP DATABASE", "TRUNCATE TABLE", "TRUNCATE", "ALTER TABLE"):
                if banned in sql_clean:
                    violations.append(
                        f"Query #{idx} violates destructive DDL ban: contains prohibited statement '{banned}'."
                    )

            # 2. Hard deletes check
            if sql_clean.startswith("DELETE FROM") or " DELETE FROM " in sql_clean:
                violations.append(
                    f"Query #{idx} violates soft-delete mandate: hard DELETE statement detected."
                )

            # 3. Missing WHERE clause on UPDATE
            if sql_clean.startswith("UPDATE") or " UPDATE " in sql_clean:
                if "WHERE" not in sql_clean:
                    violations.append(
                        f"Query #{idx} is an unconstrained mutation: UPDATE statement lacks a WHERE clause."
                    )
                elif re.search(r"WHERE\s+1\s*=\s*1", sql_clean):
                    violations.append(
                        f"Query #{idx} is an unconstrained mutation: UPDATE uses tautological 'WHERE 1=1'."
                    )

        return violations

    async def evaluate_plan(
        self,
        plan: CompliancePlan,
        sanitized_prompt: str,
        schema_context: dict[str, Any],
    ) -> JudgeEvaluation:
        """
        Evaluates a CompliancePlan and returns a structured JudgeEvaluation.
        """
        # 1. Execute deterministic pre-checks
        deterministic_violations = self.run_deterministic_prechecks(plan)
        if deterministic_violations:
            feedback_str = (
                "AUTOMATED SECURITY AUDIT FAILED (Critical Deterministic Violations):\n"
                + "\n".join(f"- {v}" for v in deterministic_violations)
            )
            return JudgeEvaluation(
                score=0.10,
                passed=False,
                verdict="REJECTED_UNSAFE",
                feedback=feedback_str,
                detected_violations=deterministic_violations,
                recommendations=[
                    "Ensure all UPDATE queries include specific, non-trivial WHERE clauses.",
                    "Never generate DROP, TRUNCATE, or ALTER statements.",
                    "Convert all deletions into soft-delete updates (`is_deleted = TRUE`).",
                ],
            )

        # 2. If deterministic checks pass, invoke LLM-as-a-Judge for semantic review
        schema_spec = json.dumps(JudgeEvaluation.model_json_schema(), indent=2)
        system_instruction = JUDGE_SYSTEM_PROMPT.format(schema_json=schema_spec)

        user_content = f"""USER COMPLIANCE GOAL:
{sanitized_prompt}

PROPOSED COMPLIANCE PLAN:
{json.dumps(plan.model_dump(), indent=2, default=str)}

TARGET DATABASE SCHEMA CONTEXT:
{json.dumps(schema_context, indent=2, default=str)}
"""

        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_content},
        ]

        llm = self._get_llm()
        response = await llm.ainvoke(messages)
        raw_text = response.content.strip()

        try:
            evaluation = JudgeEvaluation.model_validate_json(raw_text)
        except Exception as e:
            logger.warning(f"Error parsing JudgeEvaluation JSON directly: {e}. Attempting regex recovery.")
            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if match:
                evaluation = JudgeEvaluation.model_validate_json(match.group(0))
            else:
                raise ValueError(f"Judge LLM failed to produce valid JSON: {raw_text}") from e

        # Enforce consistency: if score is below 0.85, passed must be False
        if evaluation.score < 0.85:
            evaluation.passed = False
            if evaluation.verdict == "VERIFIED_SAFE":
                evaluation.verdict = "REVISION_REQUIRED"

        return evaluation


# Global singleton instance
safety_judge = SafetyJudge()


# ==============================================================================
# LangGraph Safety Judge Node Function
# ==============================================================================


async def judge_node(state: ComplianceState) -> dict[str, Any]:
    """
    LangGraph execution node for the Safety Judge Agent.
    
    1. Ingests proposed_plan from state.
    2. Runs hybrid evaluation via SafetyJudge.
    3. If approved (score >= 0.85):
       - Sets status to 'PENDING_APPROVAL' (ready for HITL token issuance and interruption).
    4. If rejected or revision requested:
       - If replan_count < 3: increments replan_count, sets status to 'PENDING_REASONING' to loop back to planner.
       - If replan_count >= 3: sets status to 'FAILED', logs fatal rejection.
    """
    plan_dict = state.get("proposed_plan")
    if not plan_dict:
        raise ValueError("Cannot execute judge_node: state contains no 'proposed_plan'.")

    plan = CompliancePlan.model_validate(plan_dict)
    prompt = state.get("sanitized_prompt") or state.get("original_prompt", "")
    schema_context = state.get("schema_context", {})
    replan_count = state.get("replan_count", 0)

    # Evaluate plan
    evaluation = await safety_judge.evaluate_plan(
        plan=plan,
        sanitized_prompt=prompt,
        schema_context=schema_context,
    )

    update: dict[str, Any] = {
        "judge_evaluation": evaluation.model_dump(),
    }

    if evaluation.passed:
        update["status"] = "PENDING_APPROVAL"
        summary_msg = (
            f"[Safety Judge]: Plan APPROVED with score {evaluation.score:.2f} ({evaluation.verdict}). "
            f"Findings: {evaluation.feedback}. Transitioning to PENDING_APPROVAL for Human-in-the-Loop review."
        )
    else:
        new_count = replan_count + 1
        update["replan_count"] = new_count
        if new_count < 3:
            update["status"] = "PENDING_REASONING"
            summary_msg = (
                f"[Safety Judge]: Plan REJECTED with score {evaluation.score:.2f} ({evaluation.verdict}). "
                f"Violations: {', '.join(evaluation.detected_violations) or 'None'}. "
                f"Triggering reflection loop (attempt {new_count}/3). Feedback: {evaluation.feedback}"
            )
        else:
            update["status"] = "FAILED"
            update["error_message"] = (
                f"Safety Judge permanently rejected plan after {new_count} attempts: {evaluation.feedback}"
            )
            summary_msg = (
                f"[Safety Judge]: Maximum replan limit reached (3/3). Pipeline FAILED. "
                f"Score: {evaluation.score:.2f}. Reason: {evaluation.feedback}"
            )

    update["messages"] = [AIMessage(content=summary_msg)]
    return update
