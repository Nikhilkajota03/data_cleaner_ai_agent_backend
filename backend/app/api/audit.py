"""
Audit & Governance Introspection Endpoints.

Handles:
- GET /api/audit/executions: Execution history joined with plan + thread metadata.
- GET /api/audit/executions/{execution_id}: Plan queries and per-row before/after diffs.
- GET /api/audit/crm-stats: Dirty-data quality counters over staging_db.
- GET /api/audit/retention-stats: Soft-deleted / purge-eligible counters.
"""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import (
    AgentExecution,
    ExecutionPlan,
    RowPreImage,
    SupervisorThread,
    WorkflowStatus,
)
from backend.app.db.session import get_gov_db, get_staging_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["Audit & Governance"])

# Whitelist guards the dynamic table name used for the "after" lookup.
_ALLOWED_TABLES = {"clients", "invoices"}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _summarize(
    execution: AgentExecution,
    plan: ExecutionPlan,
    thread: SupervisorThread,
    pre_image_count: int,
) -> dict[str, Any]:
    output = execution.agent_output or {}
    plan_json = plan.proposed_sql_plan or {}
    rolled_back = plan.status == WorkflowStatus.ROLLED_BACK
    effective_status = "ROLLED_BACK" if rolled_back else execution.status.value
    return {
        "execution_id": str(execution.execution_id),
        "plan_id": str(plan.plan_id),
        "thread_id": str(thread.thread_id),
        "master_goal": thread.master_goal,
        "status": effective_status,
        "approved_by": plan.approved_by,
        "approved_at": _iso(plan.approved_at),
        "judge_score": float(plan.judge_score) if plan.judge_score is not None else None,
        "plan_summary": plan_json.get("summary"),
        "affected_rows": output.get("total_affected_rows", output.get("affected_rows")),
        "pre_images_captured": pre_image_count,
        "started_at": _iso(execution.started_at),
        "completed_at": _iso(execution.completed_at),
        "can_rollback": (
            execution.status == WorkflowStatus.COMPLETED
            and not rolled_back
            and pre_image_count > 0
        ),
    }


@router.get("/executions", summary="List execution audit history")
async def list_executions(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    gov_db: AsyncSession = Depends(get_gov_db),
) -> list[dict[str, Any]]:
    stmt = (
        select(AgentExecution, ExecutionPlan, SupervisorThread)
        .join(ExecutionPlan, AgentExecution.plan_id == ExecutionPlan.plan_id)
        .join(SupervisorThread, ExecutionPlan.thread_id == SupervisorThread.thread_id)
        .order_by(AgentExecution.started_at.desc())
        .offset(skip)
        .limit(limit)
    )
    rows = (await gov_db.execute(stmt)).all()
    if not rows:
        return []

    ids = [r[0].execution_id for r in rows]
    count_stmt = (
        select(RowPreImage.execution_id, func.count())
        .where(RowPreImage.execution_id.in_(ids))
        .group_by(RowPreImage.execution_id)
    )
    counts = {eid: c for eid, c in (await gov_db.execute(count_stmt)).all()}

    return [_summarize(e, p, t, counts.get(e.execution_id, 0)) for e, p, t in rows]


@router.get("/executions/{execution_id}", summary="Execution detail with pre-image diffs")
async def get_execution_detail(
    execution_id: str,
    gov_db: AsyncSession = Depends(get_gov_db),
    staging_db: AsyncSession = Depends(get_staging_db),
) -> dict[str, Any]:
    try:
        exec_uuid = uuid.UUID(execution_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Execution not found.")

    stmt = (
        select(AgentExecution, ExecutionPlan, SupervisorThread)
        .join(ExecutionPlan, AgentExecution.plan_id == ExecutionPlan.plan_id)
        .join(SupervisorThread, ExecutionPlan.thread_id == SupervisorThread.thread_id)
        .where(AgentExecution.execution_id == exec_uuid)
    )
    row = (await gov_db.execute(stmt)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Execution not found.")
    execution, plan, thread = row

    pre_images = (
        (
            await gov_db.execute(
                select(RowPreImage)
                .where(RowPreImage.execution_id == exec_uuid)
                .order_by(RowPreImage.created_at)
                .limit(500)
            )
        )
        .scalars()
        .all()
    )

    diffs: list[dict[str, Any]] = []
    for pi in pre_images:
        before = pi.pre_image_state or {}
        after: dict[str, Any] | None = None
        if pi.target_table in _ALLOWED_TABLES:
            res = await staging_db.execute(
                text(f"SELECT row_to_json(t) FROM {pi.target_table} t WHERE id::text = :pk"),
                {"pk": pi.primary_key_value},
            )
            after = res.scalar_one_or_none()
        changed = (
            sorted(k for k in before if str(before.get(k)) != str((after or {}).get(k)))
            if after is not None
            else sorted(before.keys())
        )
        diffs.append(
            {
                "pre_image_id": str(pi.pre_image_id),
                "target_table": pi.target_table,
                "primary_key_value": pi.primary_key_value,
                "before": before,
                "after": after,
                "changed_fields": changed,
            }
        )

    query_results = (execution.agent_output or {}).get("query_results") or []
    queries = (plan.proposed_sql_plan or {}).get("queries", [])

    def _to_sql_str(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("sql_statement") or item.get("sql") or "")
        return str(item)

    sql_strings = [_to_sql_str(q).strip() for q in queries if _to_sql_str(q).strip()]
    is_read_only = len(sql_strings) > 0 and all(s.upper().startswith("SELECT") for s in sql_strings)

    # For read-only queries with empty diffs, retrieve rows if not already stored
    if not query_results and is_read_only:
        raw_exec_queries = (execution.sub_task_input or {}).get("queries") or queries
        exec_queries = [_to_sql_str(q).strip() for q in raw_exec_queries if _to_sql_str(q).strip()]
        for clean_q in exec_queries:
            if "[MASKED_" in clean_q and thread.master_goal:
                import re
                emails = re.findall(r'[\w\.-]+@[\w\.-]+', thread.master_goal)
                for idx, email in enumerate(emails, 1):
                    clean_q = clean_q.replace(f"[MASKED_EMAIL_ADDRESS_{idx}]", email)
                    clean_q = clean_q.replace("[MASKED_EMAIL_ADDRESS]", email)
            try:
                res = await staging_db.execute(text(clean_q))
                rows = [dict(r._mapping) for r in res.fetchall()]
                for r in rows:
                    query_results.append({
                        k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if isinstance(v, (uuid.UUID, bytes)) else v)
                        for k, v in r.items()
                    })
            except Exception as e:
                logger.warning(f"Could not fetch read-only query results on-the-fly: {e}")

    summary = _summarize(execution, plan, thread, len(pre_images))
    summary["queries"] = queries
    summary["judge_feedback"] = plan.judge_feedback
    summary["diffs"] = diffs
    summary["query_results"] = query_results
    summary["is_read_only"] = is_read_only
    return summary


@router.get("/crm-stats", summary="Dirty-data quality counters")
async def crm_stats(staging_db: AsyncSession = Depends(get_staging_db)) -> dict[str, int]:
    async def scalar(sql: str) -> int:
        return int((await staging_db.execute(text(sql))).scalar() or 0)

    return {
        "total_clients": await scalar("SELECT count(*) FROM clients WHERE is_deleted = false"),
        "soft_deleted_clients": await scalar("SELECT count(*) FROM clients WHERE is_deleted = true"),
        "missing_gdpr_consent": await scalar(
            "SELECT count(*) FROM clients WHERE is_deleted = false AND gdpr_consent = false"
        ),
        "malformed_phones": await scalar(
            "SELECT count(*) FROM clients WHERE is_deleted = false "
            "AND (phone IS NULL OR phone !~ '^\\+[1-9][0-9]{6,14}$')"
        ),
        "total_invoices": await scalar("SELECT count(*) FROM invoices WHERE is_deleted = false"),
        "unstandardized_invoices": await scalar(
            "SELECT count(*) FROM invoices WHERE is_deleted = false AND amount_standardized IS NULL"
        ),
    }


@router.get("/retention-stats", summary="Soft-delete retention counters")
async def retention_stats(
    retention_days: int = Query(default=30, ge=0, le=3650),
    staging_db: AsyncSession = Depends(get_staging_db),
) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    async def scalar(sql: str, **params: Any) -> int:
        return int((await staging_db.execute(text(sql), params)).scalar() or 0)

    return {
        "retention_days": retention_days,
        "cutoff_timestamp": cutoff.isoformat(),
        "soft_deleted_clients": await scalar("SELECT count(*) FROM clients WHERE is_deleted = true"),
        "soft_deleted_invoices": await scalar("SELECT count(*) FROM invoices WHERE is_deleted = true"),
        "purge_eligible_clients": await scalar(
            "SELECT count(*) FROM clients WHERE is_deleted = true AND deleted_at < :c", c=cutoff
        ),
        "purge_eligible_invoices": await scalar(
            "SELECT count(*) FROM invoices WHERE is_deleted = true AND deleted_at < :c", c=cutoff
        ),
    }
