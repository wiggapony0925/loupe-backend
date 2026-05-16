"""Card search + lookup endpoints (read-only)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.enums import TcgEnum
from app.schemas.card import CardRead
from app.schemas.common import Pagination
from app.services import card_catalog_service

router = APIRouter(prefix="/cards", tags=["cards"])


@router.get("", response_model=Pagination[CardRead], summary="Search cards")
async def search(
    q: str | None = Query(None, max_length=120),
    tcg: TcgEnum | None = None,
    set_code: str | None = Query(None, max_length=40),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Pagination[CardRead]:
    rows, total = await card_catalog_service.search_cards(
        db, q=q, tcg=tcg, set_code=set_code, page=page, page_size=page_size
    )
    return Pagination[CardRead](
        items=[CardRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{card_id}", response_model=CardRead, summary="Get one card")
async def get_one(card_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> CardRead:
    card = await card_catalog_service.get_card(db, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return CardRead.model_validate(card)


__all__ = ["router"]
