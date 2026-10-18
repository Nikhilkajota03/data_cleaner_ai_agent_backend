"""
Staging CRM Introspection Endpoints.

Handles:
- GET /api/crm/clients: Read paginated client records to inspect dirty vs cleaned states.
- GET /api/crm/clients/{client_id}: Inspect detailed client data with associated invoices.
- GET /api/crm/invoices: Read paginated invoice records with currency/amount formatting status.
"""

import logging
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.schemas import ClientDetailOut, ClientOut, InvoiceOut
from backend.app.db.models import Client, Invoice
from backend.app.db.session import get_staging_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/crm", tags=["Staging CRM Introspection"])


@router.get(
    "/clients",
    response_model=list[ClientOut],
    summary="List client records from staging_db",
)
async def list_clients(
    skip: int = Query(default=0, ge=0, description="Offset pagination"),
    limit: int = Query(default=50, ge=1, le=200, description="Page limit"),
    include_deleted: bool = Query(default=False, description="Include soft-deleted records"),
    search: Optional[str] = Query(default=None, description="Search term matching name, email, phone, or city"),
    staging_db: AsyncSession = Depends(get_staging_db),
) -> list[ClientOut]:
    """
    Retrieves paginated client entities from the business staging database.
    Allows inspection of raw vs cleaned records before and after agent execution.
    """
    stmt = select(Client)

    if not include_deleted:
        stmt = stmt.where(Client.is_deleted.is_(False))

    if search:
        search_filter = f"%{search}%"
        stmt = stmt.where(
            or_(
                Client.full_name.ilike(search_filter),
                Client.email.ilike(search_filter),
                Client.phone.ilike(search_filter),
                Client.city.ilike(search_filter),
            )
        )

    stmt = stmt.order_by(Client.created_at.desc()).offset(skip).limit(limit)
    res = await staging_db.execute(stmt)
    clients = res.scalars().all()
    return list(clients)


@router.get(
    "/clients/{client_id}",
    response_model=ClientDetailOut,
    summary="Get single client with associated invoices",
)
async def get_client_detail(
    client_id: uuid.UUID,
    include_deleted: bool = Query(default=False, description="Include if soft-deleted"),
    staging_db: AsyncSession = Depends(get_staging_db),
) -> ClientDetailOut:
    """
    Retrieves full details for a client, including their associated invoices.
    """
    stmt = (
        select(Client)
        .options(selectinload(Client.invoices))
        .where(Client.id == client_id)
    )

    if not include_deleted:
        stmt = stmt.where(Client.is_deleted.is_(False))

    res = await staging_db.execute(stmt)
    client_obj = res.scalar_one_or_none()

    if not client_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Client '{client_id}' not found.",
        )

    return client_obj


@router.get(
    "/invoices",
    response_model=list[InvoiceOut],
    summary="List invoice records from staging_db",
)
async def list_invoices(
    skip: int = Query(default=0, ge=0, description="Offset pagination"),
    limit: int = Query(default=50, ge=1, le=200, description="Page limit"),
    client_id: Optional[uuid.UUID] = Query(default=None, description="Filter by client ID"),
    include_deleted: bool = Query(default=False, description="Include soft-deleted records"),
    staging_db: AsyncSession = Depends(get_staging_db),
) -> list[InvoiceOut]:
    """
    Retrieves paginated invoice entities to verify amount standardization
    and currency normalization.
    """
    stmt = select(Invoice)

    if not include_deleted:
        stmt = stmt.where(Invoice.is_deleted.is_(False))

    if client_id:
        stmt = stmt.where(Invoice.client_id == client_id)

    stmt = stmt.order_by(Invoice.invoice_date.desc()).offset(skip).limit(limit)
    res = await staging_db.execute(stmt)
    invoices = res.scalars().all()
    return list(invoices)
