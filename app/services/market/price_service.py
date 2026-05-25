"""Price-history read service backed by ``price_snapshots``."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import GradeHouseEnum, PriceSourceEnum
from app.models.price import PriceSnapshot


async def list_snapshots(
    db: AsyncSession,
    *,
    card_id: uuid.UUID,
    house: GradeHouseEnum | None = None,
    grade: Decimal | None = None,
    source: PriceSourceEnum | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[PriceSnapshot], int]:
    stmt = select(PriceSnapshot).where(PriceSnapshot.card_id == card_id)
    count_stmt = (
        select(func.count())
        .select_from(PriceSnapshot)
        .where(PriceSnapshot.card_id == card_id)
    )
    if house is not None:
        stmt = stmt.where(PriceSnapshot.house == house)
        count_stmt = count_stmt.where(PriceSnapshot.house == house)
    if grade is not None:
        stmt = stmt.where(PriceSnapshot.grade == grade)
        count_stmt = count_stmt.where(PriceSnapshot.grade == grade)
    if source is not None:
        stmt = stmt.where(PriceSnapshot.source == source)
        count_stmt = count_stmt.where(PriceSnapshot.source == source)
    stmt = (
        stmt.order_by(
            PriceSnapshot.sale_date.desc().nulls_last(), PriceSnapshot.created_at.desc()
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).scalars().all()
    total = int((await db.execute(count_stmt)).scalar_one())
    return list(rows), total


__all__ = ["list_snapshots"]
