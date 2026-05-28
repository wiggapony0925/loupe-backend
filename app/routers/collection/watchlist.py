"""Watchlist endpoints (`/v1/watchlist`)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.schemas.watchlist import WatchlistAdd, WatchlistItemRead
from app.services.collection import watchlist_service

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


@router.get(
    "",
    response_model=list[WatchlistItemRead],
    summary="List my watchlist",
    description=(
        "Returns every card the signed-in user has pinned, most-recent first."
    ),
)
async def list_mine(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[WatchlistItemRead]:
    return await watchlist_service.list_for_user(db, user)


@router.post(
    "",
    response_model=WatchlistItemRead,
    status_code=status.HTTP_201_CREATED,
    summary="Pin a card to my watchlist",
    description=(
        "Idempotent. Returns 201 with the existing row when the card was "
        "already pinned."
    ),
)
async def create(
    payload: WatchlistAdd,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> WatchlistItemRead:
    return await watchlist_service.add(db, user, payload.card_id)


@router.delete(
    "/{card_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unpin a card from my watchlist",
)
async def delete(
    card_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    ok = await watchlist_service.remove(db, user, card_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Card not on watchlist")


__all__ = ["router"]
