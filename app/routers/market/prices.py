"""Price-history endpoints (read-only)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.card import Card
from app.models.enums import GradeHouseEnum, PriceSourceEnum
from app.schemas.common import Pagination
from app.schemas.price import PriceSnapshotRead
from app.services.market import price_service

router = APIRouter(prefix="/prices", tags=["prices"])


@router.get(
    "", response_model=Pagination[PriceSnapshotRead], summary="List price snapshots"
)
async def list_snapshots(
    card_id: uuid.UUID = Query(..., description="Card to fetch prices for."),
    house: GradeHouseEnum | None = None,
    grade: Decimal | None = Query(None, ge=0, le=10),
    source: PriceSourceEnum | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Pagination[PriceSnapshotRead]:
    # An unknown card and a card with no observed prices both used to answer
    # 200 with an empty page, leaving the client unable to tell them apart.
    # Every sibling card read (grade-summary, marketplace-prices, canonical,
    # valuation) 404s on an unknown id, so this one does too.
    if await db.get(Card, card_id) is None:
        raise HTTPException(status_code=404, detail="Card not found")
    rows, total = await price_service.list_snapshots(
        db,
        card_id=card_id,
        house=house,
        grade=grade,
        source=source,
        page=page,
        page_size=page_size,
    )
    return Pagination[PriceSnapshotRead](
        items=[PriceSnapshotRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


__all__ = ["router"]
