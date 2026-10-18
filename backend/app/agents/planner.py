"""
Compliance Planner Agent: Deterministic SQL Synthesis & Migration Planning.

This agent:
1. Introspects the target PostgreSQL database schema and data samples via the FastMCP service.
2. Ingests the sanitized user prompt (safe from PII).
3. Uses Groq LLMs (qwen/qwen3.8-27b) with guaranteed JSON Schema mode to synthesize
   a deterministic, auditable CompliancePlan.
4. Enforces strict enterprise rules:
   - Mandatory WHERE clauses on all mutations.
   - Strict prohibition of destructive statements (DROP, TRUNCATE, ALTER).
   - Soft-delete semantics for any record removal.
   - Support for iterative reflection loops if the Safety Judge requests a revision.
"""

import json
import logging
import re
from typing import Any, Optional
import uuid

import httpx
from langchain_core.messages import AIMessage
from langchain_groq import ChatGroq

from backend.app.agents.state import CompliancePlan, ComplianceState
from backend.app.core.config import settings

logger = logging.getLogger(__name__)


# ==============================================================================
# System Prompt & Schema Instructions
# ==============================================================================

PLANNER_SYSTEM_PROMPT = """You are the Principal Database Compliance Architect and SQL Migration Planner for an Enterprise CRM.
Your objective is to inspect the provided target database schema, analyze the sanitized user goal, and generate an exact, auditable data cleaning and normalization plan in valid JSON format.

CRITICAL ENTERPRISE SAFETY RULES:
1. ZERO UNCONSTRAINED MUTATIONS: Every UPDATE query MUST have an explicit, non-trivial WHERE clause (e.g. scoping by primary key `id` or exact dirty values). Never omit WHERE clauses. Never use tautologies like 1=1.
2. ZERO DESTRUCTIVE OPERATIONS: You are strictly FORBIDDEN from generating DROP TABLE, TRUNCATE, or ALTER TABLE.
3. SOFT DELETIONS ONLY: If a deletion is requested, convert it to a soft delete: UPDATE table_name SET is_deleted = TRUE, deleted_at = CURRENT_TIMESTAMP WHERE ...;
4. STANDARDIZATION DOMAINS:
   - Country codes: Standardize to ISO-3166 alpha-2 (e.g., 'Deutschland' -> 'DE', 'germany' -> 'DE', 'France' -> 'FR').
   - Addresses: Standardize capitalization (Title Case / Upper Case as appropriate).
   - Invoices: Standardize currency ('EUR') and numeric amounts.
5. PRESERVE PII PLACEHOLDER TOKENS: If the prompt contains masked tokens like [MASKED_EMAIL_ADDRESS_1] or [MASKED_PERSON_1], preserve those exact tokens in your SQL queries. Do not alter token strings.
6. YOU MUST OUTPUT VALID JSON ONLY: Your output must strictly match the following JSON Schema:
7. ROBUST DIRTY DATA MATCHING: Staging data in this CRM frequently contains uncleaned leading/trailing whitespace and mixed casing. When searching, filtering, or updating records by email, name, or phone in WHERE clauses, ALWAYS use case-insensitive and whitespace-tolerant matching (e.g. TRIM(email) ILIKE '[MASKED_EMAIL_ADDRESS_1]' or email ILIKE '%[MASKED_EMAIL_ADDRESS_1]%') so uncleaned whitespace never prevents matching.

{schema_json}
"""


class CompliancePlanner:
    """
    Synthesizes structured compliance and normalization plans using Groq LLM.
    """

    def __init__(self, model_name: Optional[str] = None) -> None:
        self.model_name = model_name or settings.GROQ_PLANNER_MODEL
        self.fastmcp_url = settings.FASTMCP_SERVICE_URL

    def _get_llm(self) -> ChatGroq:
        return ChatGroq(
            api_key=settings.GROQ_API_KEY,
            model=self.model_name,
            temperature=0.0,
            model_kwargs={"response_format": {"type": "json_object"}},
        )

    async def fetch_schema_context(
        self, table_name: Optional[str] = None
    ) -> dict[str, Any]:
        """
        Fetches live table schema and sample preview rows from the FastMCP service.
        """
        endpoint = f"{self.fastmcp_url}/tools/schema"
        params = {"table_name": table_name} if table_name else {}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(endpoint, params=params)
                if response.status_code == 200:
                    return response.json()
                logger.warning(
                    f"FastMCP schema returned status {response.status_code}: {response.text}"
                )
        except Exception as e:
            logger.warning(f"Could not reach FastMCP schema endpoint at {endpoint}: {e}")

        # Fallback offline schema mirror for development/testing if container is offline
        return {
            "tables": {
                "clients": [
                    {"column_name": "id", "data_type": "uuid", "is_nullable": False},
                    {"column_name": "full_name", "data_type": "varchar", "is_nullable": False},
                    {"column_name": "email", "data_type": "varchar", "is_nullable": True},
                    {"column_name": "phone", "data_type": "varchar", "is_nullable": True},
                    {"column_name": "billing_address", "data_type": "text", "is_nullable": True},
                    {"column_name": "postal_code", "data_type": "varchar", "is_nullable": True},
                    {"column_name": "city", "data_type": "varchar", "is_nullable": True},
                    {"column_name": "country_code", "data_type": "varchar", "is_nullable": True},
                    {"column_name": "gdpr_consent", "data_type": "boolean", "is_nullable": True},
                    {"column_name": "status", "data_type": "varchar", "is_nullable": True},
                    {"column_name": "is_deleted", "data_type": "boolean", "is_nullable": True},
                    {"column_name": "deleted_at", "data_type": "timestamp with time zone", "is_nullable": True},
                ],
                "invoices": [
                    {"column_name": "id", "data_type": "uuid", "is_nullable": False},
                    {"column_name": "client_id", "data_type": "uuid", "is_nullable": True},
                    {"column_name": "invoice_number", "data_type": "varchar", "is_nullable": False},
                    {"column_name": "amount_raw", "data_type": "varchar", "is_nullable": True},
                    {"column_name": "amount_standardized", "data_type": "numeric", "is_nullable": True},
                    {"column_name": "currency", "data_type": "varchar", "is_nullable": True},
                    {"column_name": "is_deleted", "data_type": "boolean", "is_nullable": True},
                    {"column_name": "deleted_at", "data_type": "timestamp with time zone", "is_nullable": True},
                ],
            },
            "samples": {},
        }

    async def generate_plan(
        self,
        sanitized_prompt: str,
        schema_context: dict[str, Any],
        feedback_history: Optional[str] = None,
    ) -> CompliancePlan:
        """
        Prompts the LLM to synthesize a deterministic compliance plan conforming to CompliancePlan schema.
        """
        schema_spec = json.dumps(CompliancePlan.model_json_schema(), indent=2)
        system_instruction = PLANNER_SYSTEM_PROMPT.format(schema_json=schema_spec)

        user_content = f"""USER COMPLIANCE REQUEST:
{sanitized_prompt}

TARGET DATABASE SCHEMA CONTEXT:
{json.dumps(schema_context, indent=2, default=str)}
"""

        # If this is a replan iteration driven by the Safety Judge, inject critique
        if feedback_history:
            user_content += f"""
IMPORTANT: Your previous plan was REJECTED by the Safety Judge with this feedback:
{feedback_history}
Please correct all safety and compliance violations mentioned above.
"""

        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_content},
        ]

        llm = self._get_llm()
        response = await llm.ainvoke(messages)
        raw_text = response.content.strip()

        try:
            return CompliancePlan.model_validate_json(raw_text)
        except Exception as e:
            logger.error(f"Failed to parse LLM response into CompliancePlan: {e}\nRaw Output: {raw_text}")
            # Attempt to extract JSON block if surrounded by markdown fences
            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if match:
                return CompliancePlan.model_validate_json(match.group(0))
            raise ValueError(f"LLM failed to return a valid JSON CompliancePlan: {e}") from e


# Global singleton instance
compliance_planner = CompliancePlanner()


# ==============================================================================
# LangGraph Planner Node Function
# ==============================================================================


async def planner_node(state: ComplianceState) -> dict[str, Any]:
    """
    LangGraph execution node for the Compliance Planner Agent.
    
    1. Reads sanitized_prompt (or original_prompt) from state.
    2. Ensures schema_context is populated (queries FastMCP /tools/schema if missing).
    3. Checks if this is a replan iteration based on judge_evaluation.
    4. Generates structured CompliancePlan.
    5. Updates state with proposed_plan, schema_context, and conversation history.
    """
    prompt = state.get("sanitized_prompt") or state.get("original_prompt", "")

    # Fetch schema if not already present in state
    schema_context = state.get("schema_context")
    if not schema_context:
        schema_context = await compliance_planner.fetch_schema_context()

    # Check for reflection feedback from judge if replanning
    feedback: Optional[str] = None
    replan_count = state.get("replan_count", 0)
    judge_eval = state.get("judge_evaluation")
    if replan_count > 0 and judge_eval:
        feedback = judge_eval.get("feedback")

    # Generate plan
    plan = await compliance_planner.generate_plan(
        sanitized_prompt=prompt,
        schema_context=schema_context,
        feedback_history=feedback,
    )
    # Ensure every generated plan has a fresh unique UUID so it never collides with previous plans
    plan.plan_id = str(uuid.uuid4())

    query_count = len(plan.queries)
    summary_msg = (
        f"[Compliance Planner]: Generated plan '{plan.summary}' with {query_count} query/queries. "
        f"Target tables: {', '.join(plan.targeted_tables)}. "
        f"Simulated affected rows: ~{plan.simulated_affected_rows}. Rationale: {plan.rationale}"
    )

    return {
        "schema_context": schema_context,
        "proposed_plan": plan.model_dump(),
        "messages": [AIMessage(content=summary_msg)],
    }
