"""The signed-in user's inbox: ``/v1/me/notifications``.

Paginated because the list is now unbounded — it used to be a client-side
merge of three small responses, so there was nothing to page through. Read
state lives here rather than on the device so it survives a reinstall and
agrees across a user's phone, tablet and the web.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.notification import CATEGORIES
from app.models.user import User
from app.schemas.notification import (
    MarkReadRequest,
    NotificationPage,
    NotificationRead,
    UnreadCountRead,
)
from app.services import notification_service

router = APIRouter(prefix="/me/notifications", tags=["notifications"])


@router.get("", response_model=NotificationPage, summary="List my notifications")
async def list_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=notification_service.MAX_PAGE_SIZE),
    category: str | None = Query(None, description="Filter to one category."),
    unread_only: bool = Query(False),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationPage:
    # An unknown category would silently return an empty page and look like
    # "you have no notifications" — treat it as no filter instead.
    if category is not None and category not in CATEGORIES:
        category = None
    rows, total = await notification_service.list_for_user(
        db,
        user.id,
        page=page,
        page_size=page_size,
        category=category,
        unread_only=unread_only,
    )
    return NotificationPage(
        items=[NotificationRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        # Whole-inbox count, deliberately not this page's — the badge must not
        # change as the user scrolls or filters.
        unread=await notification_service.unread_count(db, user.id),
    )


@router.get(
    "/unread-count",
    response_model=UnreadCountRead,
    summary="Unread badge count",
)
async def get_unread_count(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> UnreadCountRead:
    """Cheap enough to poll — answered from the (user_id, read_at) index."""
    return UnreadCountRead(unread=await notification_service.unread_count(db, user.id))


@router.post(
    "/read",
    response_model=UnreadCountRead,
    summary="Mark notifications read",
)
async def mark_read(
    payload: MarkReadRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> UnreadCountRead:
    """Returns the resulting badge count so the client doesn't need a re-fetch."""
    if payload.all:
        await notification_service.mark_all_read(db, user.id)
    else:
        await notification_service.mark_read(db, user.id, payload.ids)
    return UnreadCountRead(unread=await notification_service.unread_count(db, user.id))


__all__ = ["router"]
