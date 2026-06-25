"""Admin card explorer + manual price override (`/v1/admin/cards`)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_super_admin
from app.db import get_db
from app.models.user import User
from app.schemas.card_admin import (
    AdminCardDetail,
    AdminCardPage,
    PriceOverrideRequest,
    PriceSnapshotRead,
)
from app.services import audit_service
from app.services.admin import card_admin_service

router = APIRouter(prefix="/cards", tags=["admin-cards"])


@router.get("", response_model=AdminCardPage, summary="Search the local card catalog")
async def search_cards(
    q: str | None = Query(None, max_length=120, description="Name, number, or set."),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AdminCardPage:
    return await card_admin_service.search(db, q=q, page=page, page_size=page_size)


@router.get(
    "/{card_id}", response_model=AdminCardDetail, summary="Full card record + prices"
)
async def get_card(
    card_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> AdminCardDetail:
    return await card_admin_service.get_detail(db, card_id)


@router.post(
    "/{card_id}/price",
    response_model=PriceSnapshotRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record a manual price override (super-admin)",
)
async def add_price_override(
    card_id: uuid.UUID,
    payload: PriceOverrideRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
) -> PriceSnapshotRead:
    snap = await card_admin_service.add_price_override(db, card_id, payload)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="card.price_override",
        target_table="price_snapshots",
        target_id=card_id,
        payload={
            "house": snap.house,
            "grade": snap.grade,
            "price_usd": snap.price_usd,
        },
    )
    return snap


__all__ = ["router"]
