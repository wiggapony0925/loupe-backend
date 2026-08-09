"""Admin control of the Community page (`/v1/admin/social`).

Today that means the FEATURED collector rail: which accounts appear on the
Community shelf, in which order. Curation lives in ``kv_cache`` (see
``app.social.services.featured``) so an operator can change the shelf from
the portal without a migration or a deploy.

An empty list is not an error — it means "no curation", and the rail falls
back to its ranking. That is the safe default: clearing the list can never
leave the Community page blank.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.social.schemas import (
    ModerationCaseRead,
    ModerationQueueRead,
    SocialUserCard,
)
from app.social.services import safety
from app.social.services._common import collection_peeks, user_card
from app.social.services.featured import (
    MAX_FEATURED,
    add_featured,
    featured_usernames,
    remove_featured,
    resolve_featured,
    set_featured,
)

router = APIRouter(prefix="/social", tags=["admin-social"])


class FeaturedView(BaseModel):
    """The curated rail as the portal needs to render it."""

    #: Handles exactly as stored, in operator order — these are the tags.
    usernames: list[str] = []
    #: Resolved collectors, same order. Shorter than `usernames` when an
    #: entry no longer resolves.
    collectors: list[SocialUserCard] = []
    #: Handles that no longer resolve (renamed, deactivated, deleted,
    #: banned). Surfaced so the operator can SEE why a tag shows no card
    #: instead of wondering why the rail is short.
    unresolved: list[str] = []
    max_featured: int = MAX_FEATURED


class FeaturedSet(BaseModel):
    usernames: list[str] = Field(default_factory=list)


class FeaturedAdd(BaseModel):
    username: str = Field(min_length=1, max_length=60)


async def _view(db: AsyncSession, admin: User) -> FeaturedView:
    handles = await featured_usernames()
    rows = await resolve_featured(db, handles)
    peeks = await collection_peeks(db, [p.user_id for p, _ in rows])
    resolved = {p.username for p, _ in rows}
    return FeaturedView(
        usernames=handles,
        collectors=[user_card(p, u, "none", peeks.get(p.user_id)) for p, u in rows],
        unresolved=[h for h in handles if h not in resolved],
    )


@router.get(
    "/featured",
    response_model=FeaturedView,
    summary="The curated Community rail",
)
async def get_featured(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FeaturedView:
    return await _view(db, admin)


@router.put(
    "/featured",
    response_model=FeaturedView,
    summary="Replace the curated rail (order is preserved)",
)
async def put_featured(
    payload: FeaturedSet,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FeaturedView:
    await set_featured(payload.usernames)
    return await _view(db, admin)


@router.post(
    "/featured",
    response_model=FeaturedView,
    summary="Feature one collector",
)
async def post_featured(
    payload: FeaturedAdd,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FeaturedView:
    """Rejects a handle that doesn't exist — a tag that can never render is
    worse than an error at the moment of typing."""
    handle = payload.username.strip().lower().lstrip("@")
    if not await resolve_featured(db, [handle]):
        raise HTTPException(status_code=404, detail=f"No collector @{handle}")
    if len(await featured_usernames()) >= MAX_FEATURED:
        raise HTTPException(
            status_code=409,
            detail=f"The rail holds at most {MAX_FEATURED} collectors",
        )
    await add_featured(handle)
    return await _view(db, admin)


@router.delete(
    "/featured/{username}",
    response_model=FeaturedView,
    summary="Remove one collector from the rail (the tag's ×)",
)
async def delete_featured(
    username: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FeaturedView:
    """Idempotent: removing a handle that isn't featured is a no-op, so a
    double-tap on the × can't 404."""
    await remove_featured(username)
    return await _view(db, admin)


# ── Moderation queue ──


@router.get(
    "/moderation",
    response_model=ModerationQueueRead,
    summary="The review queue (auto-flags + user reports, worst first)",
)
async def moderation_queue(
    status: str = Query("open", description="`open`, `dismissed` or `removed`."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ModerationQueueRead:
    """One list for both sources. A moderator asks "what needs me", not
    "which system noticed"."""
    return await safety.queue(db, status=status, limit=limit, offset=offset)


@router.post(
    "/moderation/{case_id}/resolve",
    response_model=ModerationCaseRead,
    summary="Dismiss a case, or remove what it points at",
)
async def resolve_case(
    case_id: uuid.UUID,
    action: str = Query(..., description="`dismiss` or `remove`."),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ModerationCaseRead:
    """Resolving one case answers every other open case about the same
    thing — nine duplicate reports left open is how a queue becomes noise."""
    return await safety.resolve(db, admin, case_id, action=action)


__all__ = ["router"]
