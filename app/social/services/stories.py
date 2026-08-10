"""Stories — 24-hour photos and videos, their tray, views and comments.

Three rules shape everything here.

**Expiry is a predicate, never a job.** Every read filters
``expires_at > now()``. Nothing sweeps, so nothing can be late: a story is
gone at the right second even if a cron is wedged or a deploy is halfway
through. The row survives its expiry on purpose — the author's archive is
the reason to keep it.

**The privacy gate is the feed's gate.** Stories reuse
:func:`visible_posts_predicate`'s rule, expressed against the story table:
public authors, yourself, and people you follow. There is deliberately no
*discoverable* variant, because a story is never recommended — the tray is
built from the follow graph, so the question "may we put this in front of
someone unasked" never comes up.

**Moderation is the same chokepoint.** A story's media and caption go
through :func:`safety.enforce` exactly like a post's. A surface where
content expires is precisely the one people try things on.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import ColumnElement, Row, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import is_admin_user
from app.models.user import User
from app.social import story_media
from app.social.models import (
    SocialFollow,
    SocialProfile,
    SocialStory,
    SocialStoryComment,
    SocialStoryView,
)
from app.social.schemas import (
    RelationshipState,
    StoryCommentRead,
    StoryRead,
    StoryTrayEntry,
    StoryTrayRead,
    StoryViewerRead,
)
from app.social.services._common import get_profile
from app.social.services.feed_common import author_card, relationships_to
from app.social.services.safety import TARGET_POST, enforce

#: How many live stories one account may hold. Past this the oldest is not
#: deleted — posting is refused — because silently dropping something
#: someone just recorded is worse than telling them.
MAX_LIVE_STORIES = 30

MAX_CAPTION = 280

#: Hard ceiling on rows the tray query may return (newest first, so the
#: oldest stories fall off for accounts following an enormous graph).
TRAY_ROW_CAP = 600


@dataclass(slots=True)
class NewStory:
    body: bytes
    content_type: str
    duration_ms: int | None = None


def _live() -> ColumnElement[bool]:
    """Not deleted, not expired. The half of every story query that is
    about time rather than about permission."""
    return and_(
        SocialStory.deleted_at.is_(None),
        SocialStory.expires_at > func.now(),
    )


def visible_stories_predicate(viewer_id: uuid.UUID) -> ColumnElement[bool]:
    """Whose stories this viewer may see.

    The feed's rule, restated for this table: public authors, yourself, and
    accounts you follow. Written as a predicate rather than checked per row
    so the tray, the viewer, the comments and the permalink all inherit it —
    a gate remembered at four call sites is a gate forgotten at one.
    """
    followed = select(SocialFollow.followee_id).where(
        SocialFollow.follower_id == viewer_id
    )
    return and_(
        _live(),
        SocialProfile.user_id.is_not(None),
        or_(
            SocialProfile.is_private.is_(False),
            SocialProfile.user_id == viewer_id,
            SocialProfile.user_id.in_(followed),
        ),
    )


def _base(viewer_id: uuid.UUID):
    return (
        select(SocialStory, SocialProfile, User)
        .join(SocialProfile, SocialProfile.user_id == SocialStory.author_id)
        .join(User, User.id == SocialStory.author_id)
        .where(
            User.deleted_at.is_(None),
            User.banned_at.is_(None),
            visible_stories_predicate(viewer_id),
        )
    )


# ── Writing ──


async def create_story(
    db: AsyncSession,
    author: User,
    *,
    media: NewStory,
    caption: str | None = None,
) -> StoryRead:
    """Publish a story. Expires 24 hours from now."""
    profile = await get_profile(db, author.id)
    if profile is None:
        raise HTTPException(status_code=409, detail="Claim a username before posting")

    text = (caption or "").strip()
    if len(text) > MAX_CAPTION:
        raise HTTPException(
            status_code=422, detail=f"Caption is longer than {MAX_CAPTION} characters"
        )

    live = (
        await db.execute(
            select(func.count())
            .select_from(SocialStory)
            .where(SocialStory.author_id == author.id, _live())
        )
    ).scalar_one()
    if live >= MAX_LIVE_STORIES:
        raise HTTPException(
            status_code=422,
            detail=f"You already have {MAX_LIVE_STORIES} stories up. "
            "Wait for one to expire, or delete one.",
        )

    duration = media.duration_ms
    if story_media.is_video(media.content_type):
        if duration is not None and duration > story_media.MAX_STORY_DURATION_MS:
            seconds = story_media.MAX_STORY_DURATION_MS // 1000
            raise HTTPException(
                status_code=422, detail=f"Videos can be up to {seconds} seconds"
            )
        if duration is not None and duration <= 0:
            # A zero/negative length would drive the viewer's progress bar,
            # advancing the story before its first frame renders. Store the
            # unknown, which clients already handle with a fallback dwell.
            duration = None
    else:
        # A still has no duration. Accepting one from the client would put a
        # number in the column that the progress bar would then honour.
        duration = None

    # SCREENED BEFORE ANYTHING IS WRITTEN, through the same chokepoint as a
    # post. `target_id` is provisional because nothing is stored yet — for a
    # refusal the case IS the record.
    story_id = uuid.uuid4()
    await enforce(
        db,
        actor=author,
        surface=TARGET_POST,
        target_id=story_id,
        text=text or None,
        # Video frames are not screened: the moderation API takes stills, and
        # sampling frames server-side means decoding video on the request
        # path. The caption is screened, the reporting path covers the rest.
        images=(
            []
            if story_media.is_video(media.content_type)
            else [(media.body, media.content_type)]
        ),
    )

    width, height = story_media.probe_size(media.body)
    key = await story_media.store(story_id, media.body, media.content_type)

    now = datetime.now(UTC)
    story = SocialStory(
        id=story_id,
        author_id=author.id,
        storage_key=key,
        content_type=media.content_type,
        width=width,
        height=height,
        duration_ms=duration,
        caption=text or None,
        created_at=now,
        expires_at=now + timedelta(hours=story_media.STORY_TTL_HOURS),
    )
    db.add(story)
    await db.commit()
    await db.refresh(story)

    return _to_read(story, profile, author, viewer=author, seen=True, view_count=0)


async def delete_story(db: AsyncSession, viewer: User, story_id: uuid.UUID) -> None:
    """Soft-delete. Author or staff."""
    story = await db.get(SocialStory, story_id)
    if story is None or story.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Story not found")
    if story.author_id != viewer.id and not is_admin_user(viewer):
        raise HTTPException(status_code=403, detail="Not your story")
    story.deleted_at = datetime.now(UTC)
    await db.commit()


# ── Reading ──


async def tray(db: AsyncSession, viewer: User) -> StoryTrayRead:
    """The row of avatars above the feed.

    One query for the stories, one for the viewer's seen-set, one for
    relationships. Deliberately not one query per author: the tray is the
    first thing the community tab renders, and N+1 there is N+1 on every
    single open.

    Ordered UNSEEN FIRST, then by recency. That is the ordering every story
    tray uses and the reason it works — the row is a queue of what you
    haven't watched, not a leaderboard of who posts most.
    """
    # The tray is BUILT FROM THE FOLLOW GRAPH — yourself plus accounts you
    # follow — which is what the module docstring always promised. The
    # visibility predicate alone admits every public account in the product,
    # which put strangers' rings in front of brand-new users and made the
    # row count grow with total signups rather than with who you follow.
    followed = select(SocialFollow.followee_id).where(
        SocialFollow.follower_id == viewer.id
    )
    rows = (
        await db.execute(
            _base(viewer.id)
            .where(
                or_(
                    SocialStory.author_id == viewer.id,
                    SocialStory.author_id.in_(followed),
                )
            )
            .order_by(SocialStory.created_at.desc())
            # Backstop, not pagination: newest-first means an account
            # following an enormous graph loses only the OLDEST stories,
            # and the grouping below never handles an unbounded row set.
            .limit(TRAY_ROW_CAP)
        )
    ).all()
    if not rows:
        return StoryTrayRead()

    story_ids = [story.id for story, _, _ in rows]
    seen_ids = set(
        (
            await db.execute(
                select(SocialStoryView.story_id).where(
                    SocialStoryView.viewer_id == viewer.id,
                    SocialStoryView.story_id.in_(story_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    rels = await relationships_to(
        db, viewer.id, [profile.user_id for _, profile, _ in rows]
    )

    # Group by author, keeping the newest-first order the query produced.
    grouped: dict[uuid.UUID, list[tuple[SocialStory, SocialProfile, User]]] = {}
    for story, profile, user in rows:
        grouped.setdefault(profile.user_id, []).append((story, profile, user))

    mine: StoryTrayEntry | None = None
    others: list[StoryTrayEntry] = []
    for author_id, group in grouped.items():
        newest, profile, user = group[0]
        entry = StoryTrayEntry(
            author=author_card(profile, user, rels.get(author_id, "none")),
            story_count=len(group),
            has_unseen=any(story.id not in seen_ids for story, _, _ in group),
            latest_at=newest.created_at,
            preview_url=story_media.media_url(newest.id),
            kind="video" if story_media.is_video(newest.content_type) else "image",
        )
        if author_id == viewer.id:
            # Your own ring is never lit — you've seen your own stories, and
            # the slot renders as "Your story" with a plus rather than as a
            # queue item.
            entry.has_unseen = False
            mine = entry
        else:
            others.append(entry)

    others.sort(key=lambda e: (not e.has_unseen, -e.latest_at.timestamp()))
    return StoryTrayRead(mine=mine, entries=others)


async def for_author(db: AsyncSession, viewer: User, handle: str) -> list[StoryRead]:
    """One author's live stories, oldest first — the order they're tapped
    through, which is the order they were told in."""
    rows = (
        await db.execute(
            _base(viewer.id)
            .where(SocialProfile.username == handle.strip().lstrip("@").lower())
            .order_by(SocialStory.created_at.asc())
        )
    ).all()
    if not rows:
        # Indistinguishable from "no such person" on purpose: a private
        # account's existence shouldn't be probeable through this endpoint.
        raise HTTPException(status_code=404, detail="No stories")

    return await _hydrate(db, viewer, rows)


async def archive(db: AsyncSession, viewer: User) -> list[StoryRead]:
    """The author's OWN expired stories, newest first.

    Not gated on `expires_at` — that is the point of an archive — but
    still scoped to `viewer`, so this is the one read where a story past
    its 24 hours comes back, and only ever to the person who posted it.
    """
    rows = (
        await db.execute(
            select(SocialStory, SocialProfile, User)
            .join(SocialProfile, SocialProfile.user_id == SocialStory.author_id)
            .join(User, User.id == SocialStory.author_id)
            .where(
                SocialStory.author_id == viewer.id,
                SocialStory.deleted_at.is_(None),
            )
            .order_by(SocialStory.created_at.desc())
        )
    ).all()
    return await _hydrate(db, viewer, rows)


async def _hydrate(
    db: AsyncSession,
    viewer: User,
    rows: Sequence[Row[tuple[SocialStory, SocialProfile, User]]],
) -> list[StoryRead]:
    """Turn story rows into payloads — a fixed number of queries per page,
    whatever the page size."""
    if not rows:
        return []
    ids = [story.id for story, _, _ in rows]

    seen = set(
        (
            await db.execute(
                select(SocialStoryView.story_id).where(
                    SocialStoryView.viewer_id == viewer.id,
                    SocialStoryView.story_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    views: dict[uuid.UUID, int] = {
        sid: int(n)
        for sid, n in (
            await db.execute(
                select(SocialStoryView.story_id, func.count())
                .where(SocialStoryView.story_id.in_(ids))
                .group_by(SocialStoryView.story_id)
            )
        ).all()
    }
    comments: dict[uuid.UUID, int] = {
        sid: int(n)
        for sid, n in (
            await db.execute(
                select(SocialStoryComment.story_id, func.count())
                .where(
                    SocialStoryComment.story_id.in_(ids),
                    SocialStoryComment.deleted_at.is_(None),
                )
                .group_by(SocialStoryComment.story_id)
            )
        ).all()
    }
    rels = await relationships_to(
        db, viewer.id, [profile.user_id for _, profile, _ in rows]
    )

    return [
        _to_read(
            story,
            profile,
            user,
            viewer=viewer,
            seen=story.id in seen,
            view_count=views.get(story.id, 0),
            comment_count=comments.get(story.id, 0),
            relationship=rels.get(profile.user_id, "none"),
        )
        for story, profile, user in rows
    ]


def _to_read(
    story: SocialStory,
    profile: SocialProfile,
    user: User,
    *,
    viewer: User,
    seen: bool,
    view_count: int,
    comment_count: int = 0,
    relationship: RelationshipState = "self",
) -> StoryRead:
    mine = story.author_id == viewer.id
    return StoryRead(
        id=story.id,
        author=author_card(profile, user, relationship),
        url=story_media.media_url(story.id),
        kind="video" if story_media.is_video(story.content_type) else "image",
        content_type=story.content_type,
        width=story.width,
        height=story.height,
        duration_ms=story.duration_ms,
        caption=story.caption,
        created_at=story.created_at,
        expires_at=story.expires_at,
        seen=seen,
        # Nobody gets to see how many people watched someone else's story.
        view_count=view_count if mine else 0,
        comment_count=comment_count,
        can_delete=mine or is_admin_user(viewer),
    )


# ── Views ──


async def mark_seen(db: AsyncSession, viewer: User, story_id: uuid.UUID) -> None:
    """Record that the viewer opened this story. Idempotent.

    Your own view is not recorded — an author checking their own story
    should not appear in their own viewer list, and it would make every
    story read "1 view" the moment it was posted.
    """
    row = (await db.execute(_base(viewer.id).where(SocialStory.id == story_id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Story not found")
    story, _, _ = row
    if story.author_id == viewer.id:
        return

    db.add(SocialStoryView(story_id=story_id, viewer_id=viewer.id))
    try:
        await db.commit()
    except Exception:
        # Unique violation: they've watched it before. Nothing to do, and
        # the caller must not see an error for re-watching.
        await db.rollback()


async def viewers(
    db: AsyncSession, viewer: User, story_id: uuid.UUID
) -> list[StoryViewerRead]:
    """Who opened your story. AUTHOR ONLY."""
    story = await db.get(SocialStory, story_id)
    if story is None or story.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Story not found")
    if story.author_id != viewer.id:
        raise HTTPException(status_code=403, detail="Not your story")

    rows = (
        await db.execute(
            select(SocialStoryView, SocialProfile, User)
            .join(SocialProfile, SocialProfile.user_id == SocialStoryView.viewer_id)
            .join(User, User.id == SocialStoryView.viewer_id)
            .where(SocialStoryView.story_id == story_id)
            .order_by(SocialStoryView.viewed_at.desc())
        )
    ).all()
    rels = await relationships_to(
        db, viewer.id, [profile.user_id for _, profile, _ in rows]
    )
    return [
        StoryViewerRead(
            viewer=author_card(profile, user, rels.get(profile.user_id, "none")),
            viewed_at=view.viewed_at,
        )
        for view, profile, user in rows
    ]


# ── Comments ──


async def comment(
    db: AsyncSession, author: User, story_id: uuid.UUID, body: str
) -> StoryCommentRead:
    """Leave a comment on a story.

    Visible to everyone who can see the story, and moderated like any other
    public text — see the model for why this is a comment and not a DM.
    """
    row = (await db.execute(_base(author.id).where(SocialStory.id == story_id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Story not found")

    profile = await get_profile(db, author.id)
    if profile is None:
        raise HTTPException(
            status_code=409, detail="Claim a username before commenting"
        )

    text = body.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Say something")

    comment_id = uuid.uuid4()
    await enforce(
        db,
        actor=author,
        surface=TARGET_POST,
        target_id=comment_id,
        text=text,
    )

    entry = SocialStoryComment(
        id=comment_id, story_id=story_id, author_id=author.id, body=text
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    return StoryCommentRead(
        id=entry.id,
        story_id=story_id,
        author=author_card(profile, author, "self"),
        body=entry.body,
        created_at=entry.created_at,
        can_delete=True,
    )


async def comments(
    db: AsyncSession, viewer: User, story_id: uuid.UUID
) -> list[StoryCommentRead]:
    """A story's comments, oldest first."""
    story_row = (
        await db.execute(_base(viewer.id).where(SocialStory.id == story_id))
    ).first()
    if story_row is None:
        raise HTTPException(status_code=404, detail="Story not found")
    story, _, _ = story_row

    rows = (
        await db.execute(
            select(SocialStoryComment, SocialProfile, User)
            .join(SocialProfile, SocialProfile.user_id == SocialStoryComment.author_id)
            .join(User, User.id == SocialStoryComment.author_id)
            .where(
                SocialStoryComment.story_id == story_id,
                SocialStoryComment.deleted_at.is_(None),
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
            )
            .order_by(SocialStoryComment.created_at.asc())
        )
    ).all()
    rels = await relationships_to(
        db, viewer.id, [profile.user_id for _, profile, _ in rows]
    )
    staff = is_admin_user(viewer)
    return [
        StoryCommentRead(
            id=entry.id,
            story_id=story_id,
            author=author_card(profile, user, rels.get(profile.user_id, "none")),
            body=entry.body,
            created_at=entry.created_at,
            # The story's author can clear anything under their own story —
            # it is their space, and they can't mute a DM they never got.
            can_delete=(
                entry.author_id == viewer.id or story.author_id == viewer.id or staff
            ),
        )
        for entry, profile, user in rows
    ]


async def delete_comment(db: AsyncSession, viewer: User, comment_id: uuid.UUID) -> None:
    """Remove a comment. Its author, the story's author, or staff."""
    entry = await db.get(SocialStoryComment, comment_id)
    if entry is None or entry.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Comment not found")
    story = await db.get(SocialStory, entry.story_id)
    owner = story is not None and story.author_id == viewer.id
    if entry.author_id != viewer.id and not owner and not is_admin_user(viewer):
        raise HTTPException(status_code=403, detail="Not yours to remove")
    entry.deleted_at = datetime.now(UTC)
    await db.commit()


async def purge_expired_media(db: AsyncSession, *, older_than_days: int = 30) -> int:
    """Drop the ROWS for stories long past expiry. Returns the count.

    Not part of any read path — expiry is already handled by the predicate.
    This exists so the archive doesn't grow forever, and is deliberately
    conservative: a month past expiry, well beyond any reasonable "where
    did my story go".
    """
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    result = await db.execute(
        delete(SocialStory).where(SocialStory.expires_at < cutoff)
    )
    await db.commit()
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "MAX_CAPTION",
    "MAX_LIVE_STORIES",
    "NewStory",
    "archive",
    "comment",
    "comments",
    "create_story",
    "delete_comment",
    "delete_story",
    "for_author",
    "mark_seen",
    "purge_expired_media",
    "tray",
    "viewers",
    "visible_stories_predicate",
]
