"""Watchlist CRUD — user-pinned cards.

One row per (user, card) pair, unbounded. `add`/`remove` accept a local Card
UUID *or* a composite upstream id (`pokemontcg:base1-4`) and resolve +
materialize it server-side, so the card-detail "heart toggle" works straight
off the browse/search view — which only knows the upstream id — without any
client-side resolve round-trip. `add` is idempotent.
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
from app.services.catalog import card_resolver_service


class CardNotResolvable(Exception):
    """Raised when a watchlist ref can't be resolved/materialized to a card."""


def _to_read(
    row: WatchlistItem, card: Card | None, upstream_id: str | None = None
) -> WatchlistItemRead:
    out = WatchlistItemRead.model_validate(row)
    out.upstream_id = upstream_id
    if card is not None:
        out.card_name = card.name
        out.card_image_url = card.image_url
    return out


async def _preferred_ref(db: AsyncSession, card_id: uuid.UUID) -> str | None:
    return (await card_resolver_service.upstream_ids_for(db, [card_id])).get(card_id)


def _pair_query(user_id: uuid.UUID, card_id: uuid.UUID):
    return (
        select(WatchlistItem, Card)
        .outerjoin(Card, Card.id == WatchlistItem.card_id)
        .where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.card_id == card_id,
        )
    )


async def list_for_user(db: AsyncSession, user: User) -> list[WatchlistItemRead]:
    rows = (
        await db.execute(
            select(WatchlistItem, Card)
            .outerjoin(Card, Card.id == WatchlistItem.card_id)
            .where(WatchlistItem.user_id == user.id)
            .order_by(WatchlistItem.created_at.desc())
        )
    ).all()
    upstream = await card_resolver_service.upstream_ids_for(
        db, [w.card_id for (w, _c) in rows]
    )
    return [_to_read(w, c, upstream.get(w.card_id)) for (w, c) in rows]


async def add(db: AsyncSession, user: User, ref: str) -> WatchlistItemRead:
    """Idempotent add — returns the existing row when already pinned.

    ``ref`` is a local Card UUID or a composite upstream id; the backend
    resolves + materializes it. Raises :class:`CardNotResolvable` when the ref
    can't be resolved to a real card.
    """
    card_id = await card_resolver_service.ensure_local_card_id(db, ref)
    if card_id is None:
        raise CardNotResolvable(ref)
    upstream_id = ref if ":" in str(ref) else await _preferred_ref(db, card_id)

    existing = (await db.execute(_pair_query(user.id, card_id))).first()
    if existing is not None:
        w, c = existing
        return _to_read(w, c, upstream_id)

    row = WatchlistItem(user_id=user.id, card_id=card_id)
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # Race with another request that just inserted the same pair.
        await db.rollback()
        existing = (await db.execute(_pair_query(user.id, card_id))).first()
        if existing is not None:
            w, c = existing
            return _to_read(w, c, upstream_id)
        raise
    await db.refresh(row)
    card = (
        await db.execute(select(Card).where(Card.id == card_id))
    ).scalar_one_or_none()
    return _to_read(row, card, upstream_id)


async def remove(db: AsyncSession, user: User, ref: str) -> bool:
    """Unpin by a local UUID or composite upstream id. False when not pinned."""
    card_id = await card_resolver_service.ensure_local_card_id(db, ref)
    if card_id is None:
        return False
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


__all__ = ["CardNotResolvable", "add", "is_watching", "list_for_user", "remove"]
