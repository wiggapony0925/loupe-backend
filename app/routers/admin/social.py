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
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.social import post_media, story_media
from app.social.models import (
    SocialPost,
    SocialPostMedia,
    SocialProfile,
    SocialStory,
    SocialStoryComment,
    SocialStoryView,
)
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
    status: str = Query(
        "open",
        description=(
            "`open`, `dismissed`, `removed` (a human removed it) or "
            "`blocked` (the classifier refused it at write — never published)."
        ),
    ),
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


# ── Stories: dev tools ──
#
# The 24-hour lifecycle is the one part of stories nobody can test by
# waiting — so the portal gets the two levers that close the loop: make
# the seeded accounts post stories, and force one to expire NOW. If the
# expiry predicate is right, an expired story leaves every live surface
# the moment the button is pressed, and re-seeding lights the tray again.


class AdminStoryRead(BaseModel):
    id: uuid.UUID
    username: str
    kind: str
    caption: str | None
    created_at: datetime
    expires_at: datetime
    live: bool
    view_count: int
    comment_count: int


class StorySeedResult(BaseModel):
    created: int
    #: Accounts skipped because they already have live stories — the
    #: retrigger contract: while a story is up, seeding is a no-op for
    #: that account; expire it (or wait 24h) and seeding works again.
    skipped_live: int
    authors: list[str]


@router.get(
    "/stories",
    response_model=list[AdminStoryRead],
    summary="Every story, live and expired",
)
async def admin_stories(
    limit: int = Query(60, ge=1, le=200),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AdminStoryRead]:
    rows = (
        await db.execute(
            select(SocialStory, SocialProfile.username)
            .join(SocialProfile, SocialProfile.user_id == SocialStory.author_id)
            .where(SocialStory.deleted_at.is_(None))
            .order_by(SocialStory.created_at.desc())
            .limit(limit)
        )
    ).all()
    ids = [story.id for story, _ in rows]
    views = {
        sid: int(n)
        for sid, n in (
            await db.execute(
                select(SocialStoryView.story_id, func.count())
                .where(SocialStoryView.story_id.in_(ids or [uuid.uuid4()]))
                .group_by(SocialStoryView.story_id)
            )
        ).all()
    }
    comments = {
        sid: int(n)
        for sid, n in (
            await db.execute(
                select(SocialStoryComment.story_id, func.count())
                .where(
                    SocialStoryComment.story_id.in_(ids or [uuid.uuid4()]),
                    SocialStoryComment.deleted_at.is_(None),
                )
                .group_by(SocialStoryComment.story_id)
            )
        ).all()
    }
    now = datetime.now(UTC)

    def _aware(value: datetime) -> datetime:
        # SQLite hands columns back naive; Postgres hands them back aware.
        # The rows were WRITTEN as UTC on both, so naive means "UTC with the
        # label lost", not "unknown".
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    return [
        AdminStoryRead(
            id=story.id,
            username=username,
            kind="video" if story_media.is_video(story.content_type) else "image",
            caption=story.caption,
            created_at=story.created_at,
            expires_at=story.expires_at,
            live=_aware(story.expires_at) > now,
            view_count=views.get(story.id, 0),
            comment_count=comments.get(story.id, 0),
        )
        for story, username in rows
    ]


@router.post(
    "/stories/seed",
    response_model=StorySeedResult,
    summary="Make the test accounts post stories",
)
async def seed_stories(
    accounts: int = Query(6, ge=1, le=12),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> StorySeedResult:
    """Stories built from each account's own recent post photos — a
    server-side blob copy, so this works in prod without fetching anything
    from outside. The card counts vary on purpose (2, 1, 3, …): a tray
    where every reel is one card never exercises the tap-through.
    """
    now = datetime.now(UTC)

    # Accounts that have posts with media and NO live story. The skip is
    # the retrigger contract — see StorySeedResult.skipped_live.
    live_authors = select(SocialStory.author_id).where(
        SocialStory.deleted_at.is_(None), SocialStory.expires_at > func.now()
    )
    candidates = (
        await db.execute(
            select(SocialProfile, func.max(SocialPost.created_at).label("last"))
            .join(SocialPost, SocialPost.author_id == SocialProfile.user_id)
            .join(SocialPostMedia, SocialPostMedia.post_id == SocialPost.id)
            .where(
                SocialPost.deleted_at.is_(None),
                SocialProfile.user_id.notin_(live_authors),
                SocialProfile.user_id != admin.id,
            )
            .group_by(SocialProfile.user_id)
            .order_by(func.max(SocialPost.created_at).desc())
            .limit(accounts)
        )
    ).all()

    skipped = (
        await db.execute(
            select(func.count(func.distinct(SocialStory.author_id))).where(
                SocialStory.deleted_at.is_(None),
                SocialStory.expires_at > func.now(),
            )
        )
    ).scalar_one()

    captions = [
        "mail day 📬",
        "fresh from the shop",
        "sunday sorting",
        "one more for the PC",
        "look at this centering",
        "grading pile update",
    ]
    counts = (2, 1, 3, 1, 2, 1, 2, 1, 3, 1, 2, 1)

    created = 0
    authors: list[str] = []
    for index, (profile, _last) in enumerate(candidates):
        media_rows = (
            (
                await db.execute(
                    select(SocialPostMedia)
                    .join(SocialPost, SocialPost.id == SocialPostMedia.post_id)
                    .where(
                        SocialPost.author_id == profile.user_id,
                        SocialPost.deleted_at.is_(None),
                    )
                    .order_by(SocialPost.created_at.desc())
                    .limit(counts[index % len(counts)])
                )
            )
            .scalars()
            .all()
        )
        if not media_rows:
            continue

        for slot, media_row in enumerate(media_rows):
            body = await post_media.load(media_row.id)
            if body is None:
                continue
            story_id = uuid.uuid4()
            await story_media.store(story_id, body, media_row.content_type)
            created_at = now - timedelta(minutes=index * 47 + slot * 13)
            db.add(
                SocialStory(
                    id=story_id,
                    author_id=profile.user_id,
                    storage_key=story_media.storage_key(story_id),
                    content_type=media_row.content_type,
                    width=media_row.width,
                    height=media_row.height,
                    duration_ms=(
                        6000 if story_media.is_video(media_row.content_type) else None
                    ),
                    caption=captions[(index + slot) % len(captions)],
                    created_at=created_at,
                    expires_at=created_at
                    + timedelta(hours=story_media.STORY_TTL_HOURS),
                )
            )
            created += 1
        authors.append(profile.username)

    await db.commit()
    return StorySeedResult(created=created, skipped_live=int(skipped), authors=authors)


@router.post(
    "/stories/{story_id}/expire",
    response_model=AdminStoryRead,
    summary="Force a story to expire NOW",
)
async def expire_story(
    story_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminStoryRead:
    """Sets `expires_at` to now. If the lifecycle is right, the story
    leaves the tray, the reel and the counts immediately — while staying
    in the author's archive, which is the other half of the contract."""
    story = await db.get(SocialStory, story_id)
    if story is None or story.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Story not found")
    profile = await db.get(SocialProfile, story.author_id)
    # A second into the PAST, not now. `expires_at > now()` compares a
    # microsecond-precise stored value against CURRENT_TIMESTAMP, which on
    # SQLite has no fractional part — set to the same second, the string
    # comparison keeps the story "live" until the clock ticks over. The
    # feed's created_at column documents the same trap.
    story.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    await db.refresh(story)
    # Expiry takes the story off the live surfaces; it does not erase what it
    # earned. Count the same way the list does (comments exclude deleted ones)
    # so the portal row keeps its engagement across the refresh.
    view_count = (
        await db.execute(
            select(func.count())
            .select_from(SocialStoryView)
            .where(SocialStoryView.story_id == story.id)
        )
    ).scalar_one()
    comment_count = (
        await db.execute(
            select(func.count())
            .select_from(SocialStoryComment)
            .where(
                SocialStoryComment.story_id == story.id,
                SocialStoryComment.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    return AdminStoryRead(
        id=story.id,
        username=profile.username if profile else "?",
        kind="video" if story_media.is_video(story.content_type) else "image",
        caption=story.caption,
        created_at=story.created_at,
        expires_at=story.expires_at,
        live=False,
        view_count=int(view_count),
        comment_count=int(comment_count),
    )


__all__ = ["router"]
