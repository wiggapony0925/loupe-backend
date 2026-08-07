"""Saved places — the shops a collector hearted.

Saves are keyed on the upstream store id, so the list survives OSM data
changing. Reading the list re-hydrates each store from the locator cache;
a store whose cache row has expired still returns with its id and name so
the row is never blank — it just lacks live distance until re-searched.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.stores import NearbyStore
from app.social.models import SavedStore

MAX_SAVED = 200


async def is_saved(db: AsyncSession, user: User | None, store_id: str) -> bool:
    if user is None:
        return False
    row = (
        await db.execute(
            select(SavedStore.id).where(
                SavedStore.user_id == user.id, SavedStore.store_id == store_id
            )
        )
    ).first()
    return row is not None


async def saved_ids(db: AsyncSession, user: User | None) -> set[str]:
    """Every store id this user has saved — one query for a whole map page."""
    if user is None:
        return set()
    rows = (
        await db.execute(
            select(SavedStore.store_id).where(SavedStore.user_id == user.id)
        )
    ).scalars()
    return set(rows)


async def save(db: AsyncSession, user: User, store_id: str) -> bool:
    """Heart a shop. Idempotent — returns the resulting saved state."""
    if await is_saved(db, user, store_id):
        return True
    db.add(SavedStore(user_id=user.id, store_id=store_id))
    await db.commit()
    return True


async def unsave(db: AsyncSession, user: User, store_id: str) -> bool:
    await db.execute(
        delete(SavedStore).where(
            SavedStore.user_id == user.id, SavedStore.store_id == store_id
        )
    )
    await db.commit()
    return False


async def list_saved(db: AsyncSession, user: User) -> list[NearbyStore]:
    """The user's saved places, newest first, hydrated from the store cache."""
    from app.services.stores import store_locator

    rows = (
        await db.execute(
            select(SavedStore)
            .where(SavedStore.user_id == user.id)
            .order_by(SavedStore.created_at.desc())
            .limit(MAX_SAVED)
        )
    ).scalars()

    out: list[NearbyStore] = []
    for row in rows:
        store = await store_locator.store_by_id(row.store_id)
        if store is None:
            # Cache row expired — keep the entry visible rather than
            # silently dropping a place the user deliberately saved.
            store = NearbyStore(
                id=row.store_id,
                name="Saved shop",
                lat=0.0,
                lng=0.0,
                distance_km=0.0,
                category="Card & game store",
            )
        store.is_saved = True
        out.append(store)
    return out


__all__ = ["is_saved", "list_saved", "save", "saved_ids", "unsave"]
