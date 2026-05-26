"""Collection endpoints (binders/decks) — thin HTTP shell over
:mod:`app.services.collection.collection_service`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.schemas.collection import (
    CollectionCreate,
    CollectionItemAdd,
    CollectionRead,
    CollectionUpdate,
)
from app.schemas.grade import GradedCardRead
from app.services.collection import collection_service

router = APIRouter(prefix="/collections", tags=["collections"])


@router.get("", response_model=list[CollectionRead], summary="List my collections")
async def list_mine(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> list[CollectionRead]:
    rows = await collection_service.list_for_user(db, user)
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
    row = await collection_service.create(db, user, payload)
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
    row = await collection_service.update(db, user, collection_id, payload)
    return CollectionRead.model_validate(row)


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    collection_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await collection_service.delete(db, user, collection_id)


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
    rows = await collection_service.list_items(db, user, collection_id)
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
    await collection_service.add_item(db, user, collection_id, payload.graded_card_id)
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
    await collection_service.remove_item(db, user, collection_id, graded_card_id)


__all__ = ["router"]
