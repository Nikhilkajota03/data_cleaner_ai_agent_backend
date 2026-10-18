"""
Data Retention and Compliance Purge Service.

Implements automated 30-day compliance retention cleanup for soft-deleted records
and authorized GDPR Article 17 (Right to Erasure) hard-delete operations
against the staging database (staging_db).
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import Client, Invoice

logger = logging.getLogger(__name__)


async def execute_retention_purge(
    session: AsyncSession,
    retention_days: int = 30,
) -> dict[str, Any]:
    """
    Permanently purges records in staging_db where is_deleted is TRUE
    and deleted_at is older than retention_days.

    Returns summary metrics of deleted rows.
    """
    cutoff_datetime = datetime.now(timezone.utc) - timedelta(days=retention_days)
    logger.info(
        f"Initiating retention purge for records soft-deleted before {cutoff_datetime.isoformat()} (retention_days={retention_days})"
    )

    # 1. Purge soft-deleted invoices older than cutoff
    stmt_invoices = delete(Invoice).where(
        Invoice.is_deleted.is_(True),
        Invoice.deleted_at < cutoff_datetime,
    )
    res_invoices = await session.execute(stmt_invoices)
    purged_invoices_count = res_invoices.rowcount or 0

    # 2. Purge soft-deleted clients older than cutoff
    stmt_clients = delete(Client).where(
        Client.is_deleted.is_(True),
        Client.deleted_at < cutoff_datetime,
    )
    res_clients = await session.execute(stmt_clients)
    purged_clients_count = res_clients.rowcount or 0

    await session.commit()

    logger.info(
        f"Retention purge complete: {purged_clients_count} clients, {purged_invoices_count} invoices purged."
    )

    return {
        "status": "SUCCESS",
        "retention_days": retention_days,
        "cutoff_timestamp": cutoff_datetime.isoformat(),
        "purged_clients": purged_clients_count,
        "purged_invoices": purged_invoices_count,
        "total_purged_rows": purged_clients_count + purged_invoices_count,
    }


async def execute_hard_delete_client(
    session: AsyncSession,
    client_id: str | uuid.UUID,
) -> dict[str, Any]:
    """
    Executes an immediate, irreversible physical removal of a client and all
    associated invoices (GDPR Article 17 Right to Erasure / hard delete).
    """
    c_uuid = uuid.UUID(str(client_id)) if not isinstance(client_id, uuid.UUID) else client_id

    # Check existence
    client_query = await session.execute(select(Client).where(Client.id == c_uuid))
    client_obj = client_query.scalar_one_or_none()

    if not client_obj:
        return {
            "status": "NOT_FOUND",
            "client_id": str(c_uuid),
            "purged": False,
            "message": f"Client with ID {c_uuid} does not exist in staging_db.",
        }

    # Delete invoices first (or cascade)
    inv_del = await session.execute(delete(Invoice).where(Invoice.client_id == c_uuid))
    invoices_purged = inv_del.rowcount or 0

    # Delete client
    client_del = await session.execute(delete(Client).where(Client.id == c_uuid))
    clients_purged = client_del.rowcount or 0

    await session.commit()
    logger.info(
        f"GDPR hard-delete executed for client {c_uuid}: {clients_purged} client row, {invoices_purged} invoices permanently purged."
    )

    return {
        "status": "SUCCESS",
        "client_id": str(c_uuid),
        "purged": True,
        "purged_clients": clients_purged,
        "purged_invoices": invoices_purged,
        "message": "Client and associated invoices permanently removed per GDPR Right to Erasure.",
    }
