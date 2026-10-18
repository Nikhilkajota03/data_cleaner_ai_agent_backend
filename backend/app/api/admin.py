"""
Admin and Retention Management Endpoints.

Handles:
- POST /api/admin/purge: Executes automated or manual 30-day compliance retention cleanup on soft-deleted rows.
- DELETE /api/admin/clients/{client_id}/hard-delete: Immediate GDPR Right to Erasure physical removal.
- POST /api/admin/seed-demo-data: Seeds dirty records into staging_db for live demonstrations and testing.
"""

from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.schemas import PurgeRequest, PurgeResponse
from backend.app.core.retention import execute_hard_delete_client, execute_retention_purge
from backend.app.db.models import Client, Invoice
from backend.app.db.session import get_staging_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin & Data Retention"])


@router.post(
    "/purge",
    response_model=PurgeResponse,
    summary="Execute compliance retention purge on soft-deleted records",
)
async def purge_retention_records(
    request: PurgeRequest = PurgeRequest(),
    staging_db: AsyncSession = Depends(get_staging_db),
) -> PurgeResponse:
    """
    Executes the compliance retention lifecycle policy.
    
    Permanently deletes all soft-deleted records from staging_db (invoices and clients)
    whose deleted_at timestamp is older than the specified retention period (default: 30 days).
    """
    result = await execute_retention_purge(
        session=staging_db,
        retention_days=request.retention_days,
    )
    return PurgeResponse(
        status=result["status"],
        retention_days=result["retention_days"],
        cutoff_timestamp=result["cutoff_timestamp"],
        purged_clients=result["purged_clients"],
        purged_invoices=result["purged_invoices"],
        total_purged_rows=result["total_purged_rows"],
    )


@router.delete(
    "/clients/{client_id}/hard-delete",
    summary="Execute immediate GDPR Article 17 Right to Erasure (hard-delete)",
)
async def hard_delete_client(
    client_id: uuid.UUID,
    staging_db: AsyncSession = Depends(get_staging_db),
) -> dict[str, Any]:
    """
    Permanently and irreversibly removes a specific client and their associated
    invoices from staging_db to fulfill a GDPR Right to Erasure request.
    """
    result = await execute_hard_delete_client(
        session=staging_db,
        client_id=client_id,
    )
    if result.get("status") == "NOT_FOUND":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.get("message"),
        )
    return result


@router.post(
    "/seed-demo-data",
    summary="Seed staging CRM with realistic dirty and soft-deleted records",
)
async def seed_demo_data(
    staging_db: AsyncSession = Depends(get_staging_db),
) -> dict[str, Any]:
    """
    Populates staging_db with representative un-standardized records for testing:
    - Inconsistent phone number formatting (+49, (0), spaces, dashes).
    - Unstandardized currency amounts ('1.250,50 €', '$450.00').
    - Missing or un-standardized country codes.
    - Soft-deleted historical records older than 30 days for retention purge testing.
    """
    # 1. Clean existing demo data if needed or add new
    now_utc = datetime.now(timezone.utc)
    old_date = now_utc - timedelta(days=35)

    client_1_id = uuid.uuid4()
    client_1 = Client(
        id=client_1_id,
        full_name="Hans Gruber",
        email="hans.gruber@nakatomi-corp.de",
        phone="+49 (0) 89 1234567",
        billing_address="Nakatomi Plaza, Floor 30",
        postal_code="80331",
        city="Munich",
        country_code="DE",
        gdpr_consent=True,
        consent_date=date(2025, 1, 15),
        status="ACTIVE",
        is_deleted=False,
    )

    client_2_id = uuid.uuid4()
    client_2 = Client(
        id=client_2_id,
        full_name="Erika Mustermann",
        email="erika.mustermann@web.de",
        phone="030-98765432",
        billing_address="Heidestraße 17",
        postal_code="10557",
        city="Berlin",
        country_code="DE",
        gdpr_consent=True,
        consent_date=date(2025, 2, 20),
        status="ACTIVE",
        is_deleted=False,
    )

    # Soft-deleted client (older than 30 days for retention purge demonstration)
    client_3_id = uuid.uuid4()
    client_3 = Client(
        id=client_3_id,
        full_name="Expired Inactive User",
        email="expired.user@legacy-company.com",
        phone="+44 20 7946 0912",
        billing_address="Old Street 99",
        postal_code="EC1V 9BP",
        city="London",
        country_code="GB",
        gdpr_consent=False,
        status="INACTIVE",
        is_deleted=True,
        deleted_at=old_date,
    )

    staging_db.add_all([client_1, client_2, client_3])

    # Add invoices
    inv_1 = Invoice(
        id=uuid.uuid4(),
        client_id=client_1_id,
        invoice_number="INV-2025-001",
        amount_raw="1.250,50 €",
        amount_standardized=1250.50,
        currency="EUR",
        invoice_date=date(2025, 3, 1),
        is_deleted=False,
    )

    inv_2 = Invoice(
        id=uuid.uuid4(),
        client_id=client_2_id,
        invoice_number="INV-2025-002",
        amount_raw="$850.00",
        amount_standardized=None,  # unstandardized
        currency="USD",
        invoice_date=date(2025, 3, 10),
        is_deleted=False,
    )

    inv_3 = Invoice(
        id=uuid.uuid4(),
        client_id=client_3_id,
        invoice_number="INV-2024-EXPIRED",
        amount_raw="99,00 €",
        amount_standardized=99.00,
        currency="EUR",
        invoice_date=date(2024, 12, 1),
        is_deleted=True,
        deleted_at=old_date,
    )

    staging_db.add_all([inv_1, inv_2, inv_3])
    await staging_db.commit()

    return {
        "status": "SUCCESS",
        "message": "Demo data successfully seeded into staging_db.",
        "clients_seeded": 3,
        "invoices_seeded": 3,
        "sample_client_ids": [str(client_1_id), str(client_2_id), str(client_3_id)],
    }
