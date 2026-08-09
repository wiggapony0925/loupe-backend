"""Stories — HTTP surface.

Mounted under the same ``/v1/social`` prefix as the feed. Kept in its own
module rather than bolted onto ``feed_router`` because the feed router is
already the longest file in the package, and stories share nothing with it
but the prefix.

Route order matters here for the same reason it does in the feed router:
``/stories/media/{id}``, ``/stories/tray`` and ``/stories/archive`` are all
declared BEFORE ``/stories/{handle}``, or FastAPI would try to read
"media", "tray" and "archive" as usernames.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.social import story_media
from app.social.models import SocialStory
from app.social.schemas import (
    StoryCommentCreate,
    StoryCommentRead,
    StoryRead,
    StoryTrayRead,
    StoryViewerRead,
)
from app.social.services import stories as stories_service

router = APIRouter(prefix="/social", tags=["social:stories"])


# ── Media bytes (public, immutable — same rule as post images) ──


@router.get(
    "/stories/media/{story_id}",
    summary="Story media bytes",
    response_class=Response,
)
async def get_story_media(
    story_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> Response:
    """Public like the avatar and post-image endpoints: an ``<img>`` or a
    native player carries no auth on any of our clients. The URL is
    unguessable, and the story AROUND it — who posted it, its comments,
    whether it still counts as live — stays behind the privacy gate.

    Served even after expiry, deliberately: the author's archive renders
    from these bytes, and a 404 the moment the clock ticks over would break
    the one screen that is supposed to outlive the story.
    """
    row = await db.get(SocialStory, story_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Story not found")
    body = await story_media.load(story_id)
    if body is None:
        raise HTTPException(status_code=404, detail="Story not found")
    return Response(
        content=body,
        media_type=row.content_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            # Lets a native player seek without re-downloading the clip.
            "Accept-Ranges": "bytes",
        },
    )


# ── The tray + the archive (before /{handle}) ──


@router.get("/stories/tray", response_model=StoryTrayRead, summary="Story tray")
async def story_tray(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> StoryTrayRead:
    """The row of avatars above the feed, composed and ORDERED server-side —
    unseen first, then by recency. Clients render it verbatim."""
    return await stories_service.tray(db, user)


@router.get(
    "/stories/archive",
    response_model=list[StoryRead],
    summary="Your expired stories",
)
async def story_archive(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[StoryRead]:
    """Your own stories, including expired ones, newest first.

    The only read in the module that ignores `expires_at` — and it is
    scoped to the caller, so an expired story only ever comes back to the
    person who posted it.
    """
    return await stories_service.archive(db, user)


# ── Posting ──


@router.post(
    "/stories",
    response_model=StoryRead,
    status_code=201,
    summary="Post a story",
)
async def create_story(
    caption: str | None = Form(
        None, max_length=stories_service.MAX_CAPTION, description="Optional text."
    ),
    duration_ms: int | None = Form(
        None, description="Video length in ms. Ignored for stills."
    ),
    media: UploadFile = File(
        ..., description="One JPEG/PNG/WebP image or MP4/QuickTime video."
    ),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> StoryRead:
    """Multipart, so the caption and the file arrive together — a two-step
    "create then attach" leaves a story with no media whenever the second
    call fails, and this one expires before anyone could fix it."""
    content_type = (media.content_type or "").lower()
    if content_type not in story_media.ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Stories must be JPEG, PNG, WebP, MP4 or QuickTime",
        )
    body = await media.read()
    if not body:
        raise HTTPException(status_code=422, detail="A story needs a photo or video")
    if len(body) > story_media.max_bytes(content_type):
        limit = story_media.max_bytes(content_type) // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"File too large (max {limit} MB)")

    return await stories_service.create_story(
        db,
        user,
        media=stories_service.NewStory(
            body=body, content_type=content_type, duration_ms=duration_ms
        ),
        caption=caption,
    )


@router.delete("/stories/{story_id}", status_code=204, summary="Delete a story")
async def delete_story(
    story_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await stories_service.delete_story(db, user, story_id)


# ── Views ──


@router.post("/stories/{story_id}/view", status_code=204, summary="Mark a story seen")
async def mark_seen(
    story_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Idempotent — re-watching doesn't inflate the count, and your own
    view is never recorded."""
    await stories_service.mark_seen(db, user, story_id)


@router.get(
    "/stories/{story_id}/viewers",
    response_model=list[StoryViewerRead],
    summary="Who saw your story",
)
async def story_viewers(
    story_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[StoryViewerRead]:
    """Author only. Nobody sees who watched someone else's story."""
    return await stories_service.viewers(db, user, story_id)


# ── Comments ──


@router.get(
    "/stories/{story_id}/comments",
    response_model=list[StoryCommentRead],
    summary="A story's comments",
)
async def story_comments(
    story_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[StoryCommentRead]:
    return await stories_service.comments(db, user, story_id)


@router.post(
    "/stories/{story_id}/comments",
    response_model=StoryCommentRead,
    status_code=201,
    summary="Comment on a story",
)
async def add_story_comment(
    story_id: uuid.UUID,
    payload: StoryCommentCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> StoryCommentRead:
    """A comment, visible to everyone who can see the story — not a DM.
    See ``SocialStoryComment`` for why."""
    return await stories_service.comment(db, user, story_id, payload.body)


@router.delete(
    "/stories/comments/{comment_id}",
    status_code=204,
    summary="Delete a story comment",
)
async def delete_story_comment(
    comment_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Its author, the story's author, or staff."""
    await stories_service.delete_comment(db, user, comment_id)


# ── One author's stories. LAST: `{handle}` would otherwise swallow every
#    literal path above it. ──


@router.get(
    "/stories/{handle}",
    response_model=list[StoryRead],
    summary="One collector's live stories",
)
async def stories_for(
    handle: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[StoryRead]:
    """Oldest first — the order they're tapped through is the order they
    were told in."""
    return await stories_service.for_author(db, user, handle)


__all__ = ["router"]
