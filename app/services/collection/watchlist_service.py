"""Watchlist CRUD — user-pinned cards.

Mirror of `price_alert_service` but with simpler semantics: one row per
(user, card) pair, unbounded. `add` is idempotent (silent no-op if the
pair already exists). `remove` accepts the bare `card_id` so the
frontend's "heart toggle" doesn't need to track the pin's own UUID.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card
from app.models.user import User
from app.models.watchlist import WatchlistItem
from app.schemas.watchlist import WatchlistItemRead


def _to_read(row: WatchlistItem, card: Card | None) -> WatchlistItemRead:
    out = WatchlistItemRead.model_validate(row)
    if card is not None:
        out.card_name = card.name
        out.card_image_url = card.image_url
    return out


async def list_for_user(db: AsyncSession, user: User) -> list[WatchlistItemRead]:
    rows = (
        await db.execute(
            select(WatchlistItem, Card)
            .outerjoin(Card, Card.id == WatchlistItem.card_id)
            .where(WatchlistItem.user_id == user.id)
            .order_by(WatchlistItem.created_at.desc())
        )
    ).all()
    return [_to_read(w, c) for (w, c) in rows]


async def add(db: AsyncSession, user: User, card_id: uuid.UUID) -> WatchlistItemRead:
    """Idempotent add — returns the existing row when already pinned."""
    existing = (
        await db.execute(
            select(WatchlistItem, Card)
            .outerjoin(Card, Card.id == WatchlistItem.card_id)
            .where(
                WatchlistItem.user_id == user.id,
                WatchlistItem.card_id == card_id,
            )
        )
    ).first()
    if existing is not None:
        w, c = existing
        return _to_read(w, c)

    row = WatchlistItem(user_id=user.id, card_id=card_id)
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # Race with another request that just inserted the same pair.
        await db.rollback()
        existing = (
            await db.execute(
                select(WatchlistItem, Card)
                .outerjoin(Card, Card.id == WatchlistItem.card_id)
                .where(
                    WatchlistItem.user_id == user.id,
                    WatchlistItem.card_id == card_id,
                )
            )
        ).first()
        if existing is not None:
            w, c = existing
            return _to_read(w, c)
        raise
    await db.refresh(row)
    card = (
        await db.execute(select(Card).where(Card.id == card_id))
    ).scalar_one_or_none()
    return _to_read(row, card)


async def remove(db: AsyncSession, user: User, card_id: uuid.UUID) -> bool:
    row = (
        await db.execute(
            select(WatchlistItem).where(
                WatchlistItem.user_id == user.id,
                WatchlistItem.card_id == card_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


async def is_watching(db: AsyncSession, user: User, card_id: uuid.UUID) -> bool:
    row = (
        await db.execute(
            select(WatchlistItem.id).where(
                WatchlistItem.user_id == user.id,
                WatchlistItem.card_id == card_id,
            )
        )
    ).scalar_one_or_none()
    return row is not None


__all__ = ["add", "is_watching", "list_for_user", "remove"]
