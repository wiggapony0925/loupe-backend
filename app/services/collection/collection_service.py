"""Collection (binder/deck) operations.

Pure CRUD over :class:`Collection` and its :class:`CollectionItem`
join rows. Extracted from :mod:`app.routers.collection.collections` so
the router stays a thin HTTP shell. All callers must already have an
authenticated :class:`User`; ownership is enforced here by joining on
``user_id``.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collection import Collection, CollectionItem
from app.models.grade import GradedCard
from app.models.user import User
from app.schemas.collection import CollectionCreate, CollectionUpdate


async def get_owned(
    db: AsyncSession, user: User, collection_id: uuid.UUID
) -> Collection:
    """Return the collection or 404 if it isn't owned by ``user``."""
    row = (
        await db.execute(
            select(Collection).where(
                Collection.id == collection_id, Collection.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    return row


async def list_for_user(db: AsyncSession, user: User) -> list[Collection]:
    return list(
        (
            await db.execute(
                select(Collection)
                .where(Collection.user_id == user.id)
                .order_by(Collection.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def create(db: AsyncSession, user: User, payload: CollectionCreate) -> Collection:
    row = Collection(
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        color=payload.color,
        is_public=payload.is_public,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update(
    db: AsyncSession,
    user: User,
    collection_id: uuid.UUID,
    payload: CollectionUpdate,
) -> Collection:
    row = await get_owned(db, user, collection_id)
    if payload.name is not None:
        row.name = payload.name
    if payload.description is not None:
        row.description = payload.description
    if payload.color is not None:
        row.color = payload.color
    if payload.is_public is not None:
        row.is_public = payload.is_public
    await db.commit()
    await db.refresh(row)
    return row


async def delete(db: AsyncSession, user: User, collection_id: uuid.UUID) -> None:
    row = await get_owned(db, user, collection_id)
    await db.delete(row)
    await db.commit()


async def list_items(
    db: AsyncSession, user: User, collection_id: uuid.UUID
) -> list[GradedCard]:
    await get_owned(db, user, collection_id)
    return list(
        (
            await db.execute(
                select(GradedCard)
                .join(CollectionItem, CollectionItem.graded_card_id == GradedCard.id)
                .where(
                    CollectionItem.collection_id == collection_id,
                    GradedCard.deleted_at.is_(None),
                )
                .order_by(CollectionItem.added_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def add_item(
    db: AsyncSession,
    user: User,
    collection_id: uuid.UUID,
    graded_card_id: uuid.UUID,
) -> None:
    """Add ``graded_card_id`` to ``collection_id``; idempotent."""
    await get_owned(db, user, collection_id)
    graded = (
        await db.execute(
            select(GradedCard).where(
                GradedCard.id == graded_card_id,
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if graded is None:
        raise HTTPException(status_code=404, detail="Graded card not found")
    existing = (
        await db.execute(
            select(CollectionItem).where(
                CollectionItem.collection_id == collection_id,
                CollectionItem.graded_card_id == graded_card_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            CollectionItem(collection_id=collection_id, graded_card_id=graded_card_id)
        )
        await db.commit()


async def remove_item(
    db: AsyncSession,
    user: User,
    collection_id: uuid.UUID,
    graded_card_id: uuid.UUID,
) -> None:
    await get_owned(db, user, collection_id)
    row = (
        await db.execute(
            select(CollectionItem).where(
                CollectionItem.collection_id == collection_id,
                CollectionItem.graded_card_id == graded_card_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Item not in collection")
    await db.delete(row)
    await db.commit()


__all__ = [
    "add_item",
    "create",
    "delete",
    "get_owned",
    "list_for_user",
    "list_items",
    "remove_item",
    "update",
]
