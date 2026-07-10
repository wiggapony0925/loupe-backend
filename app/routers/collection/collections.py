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
    CollectionItemsBulk,
    CollectionItemsBulkResult,
    CollectionItemsTransfer,
    CollectionMerge,
    CollectionRead,
    CollectionSummary,
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


@router.get(
    "/overview",
    response_model=list[CollectionSummary],
    summary="Portfolio switcher (All + collections, with counts & value)",
)
async def overview(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> list[CollectionSummary]:
    """Everything the dashboard's portfolio dropdown renders: the synthetic
    **All** entry (undeletable) plus each collection with a live card count and
    total value. Backend-owned — the client just displays it."""
    return await collection_service.overview(db, user)


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


@router.post(
    "/{collection_id}/items/bulk",
    response_model=CollectionItemsBulkResult,
    summary="Add many holdings to a collection (idempotent)",
)
async def bulk_add_items(
    collection_id: uuid.UUID,
    payload: CollectionItemsBulk,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> CollectionItemsBulkResult:
    added = await collection_service.bulk_add_items(
        db, user, collection_id, payload.graded_card_ids
    )
    return CollectionItemsBulkResult(added=added)


@router.post(
    "/{collection_id}/items/bulk-remove",
    response_model=CollectionItemsBulkResult,
    summary="Remove many holdings from a collection",
)
async def bulk_remove_items(
    collection_id: uuid.UUID,
    payload: CollectionItemsBulk,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> CollectionItemsBulkResult:
    removed = await collection_service.bulk_remove_items(
        db, user, collection_id, payload.graded_card_ids
    )
    return CollectionItemsBulkResult(removed=removed)


@router.post(
    "/{collection_id}/items/transfer",
    response_model=CollectionItemsBulkResult,
    summary="Move holdings from another collection into this one",
)
async def transfer_items(
    collection_id: uuid.UUID,
    payload: CollectionItemsTransfer,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> CollectionItemsBulkResult:
    added, removed = await collection_service.transfer_items(
        db, user, collection_id, payload.source_id, payload.graded_card_ids
    )
    return CollectionItemsBulkResult(added=added, removed=removed)


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


@router.post(
    "/{collection_id}/merge",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Merge another collection into this one",
)
async def merge(
    collection_id: uuid.UUID,
    payload: CollectionMerge,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Fold ``source_id`` into ``collection_id``: its items move over (de-duped)
    and the emptied source is deleted. Holdings are untouched."""
    await collection_service.merge(db, user, collection_id, payload.source_id)


__all__ = ["router"]
