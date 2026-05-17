"""Card-catalog read service backed by local DB; upstream sync is a worker."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.card import Card, CardSet
from app.models.enums import TcgEnum


async def search_cards(
    db: AsyncSession,
    *,
    q: str | None = None,
    tcg: TcgEnum | None = None,
    set_code: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[Card], int]:
    stmt = select(Card)
    count_stmt = select(func.count()).select_from(Card)
    if tcg is not None:
        stmt = stmt.where(Card.tcg == tcg)
        count_stmt = count_stmt.where(Card.tcg == tcg)
    if q:
        like = f"%{q.strip()}%"
        clause = or_(Card.name.ilike(like), Card.number.ilike(like))
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    if set_code:
        sub = select(CardSet.id).where(CardSet.code == set_code)
        stmt = stmt.where(Card.set_id.in_(sub))
        count_stmt = count_stmt.where(Card.set_id.in_(sub))
    stmt = stmt.order_by(Card.name).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()
    total = int((await db.execute(count_stmt)).scalar_one())
    return list(rows), total


async def get_card(db: AsyncSession, card_id: uuid.UUID) -> Card | None:
    # Eager-load the parent set so callers can render set name/code without
    # triggering a lazy-load (and to keep this safe to use in the async
    # context manager pattern used by HTTP services).
    return (
        await db.execute(
            select(Card).options(selectinload(Card.card_set)).where(Card.id == card_id)
        )
    ).scalar_one_or_none()


async def list_sets(
    db: AsyncSession,
    *,
    tcg: TcgEnum | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[CardSet], int]:
    stmt = select(CardSet)
    count_stmt = select(func.count()).select_from(CardSet)
    if tcg is not None:
        stmt = stmt.where(CardSet.tcg == tcg)
        count_stmt = count_stmt.where(CardSet.tcg == tcg)
    stmt = stmt.order_by(CardSet.name).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()
    total = int((await db.execute(count_stmt)).scalar_one())
    return list(rows), total


__all__ = ["get_card", "list_sets", "search_cards"]
