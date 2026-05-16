"""Card-set browse endpoints (read-only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.enums import TcgEnum
from app.schemas.card import CardSetRead
from app.schemas.common import Pagination
from app.services import card_catalog_service

router = APIRouter(prefix="/sets", tags=["sets"])


@router.get("", response_model=Pagination[CardSetRead], summary="List card sets")
async def list_sets(
    tcg: TcgEnum | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> Pagination[CardSetRead]:
    rows, total = await card_catalog_service.list_sets(
        db, tcg=tcg, page=page, page_size=page_size
    )
    return Pagination[CardSetRead](
        items=[CardSetRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


__all__ = ["router"]
