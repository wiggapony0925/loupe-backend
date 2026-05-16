"""Collection endpoints (binders/decks)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.collection import Collection, CollectionItem
from app.models.grade import GradedCard
from app.models.user import User
from app.schemas.collection import (
    CollectionCreate,
    CollectionItemAdd,
    CollectionRead,
    CollectionUpdate,
)
from app.schemas.grade import GradedCardRead

router = APIRouter(prefix="/collections", tags=["collections"])


async def _get_owned(
    db: AsyncSession, user: User, collection_id: uuid.UUID
) -> Collection:
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


@router.get("", response_model=list[CollectionRead], summary="List my collections")
async def list_mine(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> list[CollectionRead]:
    rows = (
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
    return [CollectionRead.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=CollectionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a collection",
)
async def create(
    payload: CollectionCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> CollectionRead:
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
    return CollectionRead.model_validate(row)


@router.patch(
    "/{collection_id}", response_model=CollectionRead, summary="Update collection"
)
async def update(
    collection_id: uuid.UUID,
    payload: CollectionUpdate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> CollectionRead:
    row = await _get_owned(db, user, collection_id)
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
    return CollectionRead.model_validate(row)


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    collection_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await _get_owned(db, user, collection_id)
    await db.delete(row)
    await db.commit()


@router.get(
    "/{collection_id}/items",
    response_model=list[GradedCardRead],
    summary="List items in a collection",
)
async def list_items(
    collection_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[GradedCardRead]:
    await _get_owned(db, user, collection_id)
    rows = (
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
    return [GradedCardRead.model_validate(r) for r in rows]


@router.post(
    "/{collection_id}/items",
    status_code=status.HTTP_201_CREATED,
    summary="Add a graded card to a collection",
)
async def add_item(
    collection_id: uuid.UUID,
    payload: CollectionItemAdd,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    await _get_owned(db, user, collection_id)
    graded = (
        await db.execute(
            select(GradedCard).where(
                GradedCard.id == payload.graded_card_id,
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
                CollectionItem.graded_card_id == payload.graded_card_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            CollectionItem(
                collection_id=collection_id, graded_card_id=payload.graded_card_id
            )
        )
        await db.commit()
    return {"status": "ok"}


@router.delete(
    "/{collection_id}/items/{graded_card_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_item(
    collection_id: uuid.UUID,
    graded_card_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _get_owned(db, user, collection_id)
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


__all__ = ["router"]
