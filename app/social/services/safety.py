"""The review queue: opening cases, taking reports, resolving them.

One queue for two sources — the classifier and the community. A moderator
asks "what needs my attention", not "which system noticed", so auto-flags
and user reports are the same row type and the same list.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.social import moderation
from app.social.models import (
    SocialModerationCase,
    SocialPost,
    SocialPostComment,
    SocialProfile,
    SocialStory,
    SocialStoryComment,
)
from app.social.schemas import (
    ModerationCaseRead,
    ModerationQueueRead,
    ReportCreate,
)
from app.utils.logger import get_logger

logger = get_logger("social.safety")

TARGET_POST = "post"
TARGET_COMMENT = "comment"
TARGET_PROFILE = "profile"
TARGET_REVIEW = "review"
TARGET_COLLECTION = "collection"
#: Stories live in their own tables, so they get their own target types. A
#: story case filed as a "post" points at an id no post table holds — the
#: queue would show a dead "view post" link and resolving it would remove
#: nothing while stamping the case handled.
TARGET_STORY = "story"
TARGET_STORY_COMMENT = "story_comment"

#: What a USER may report. Deliberately narrower than what we SCREEN —
#: nobody needs a "report this collection name" button, but the name still
#: gets screened on write.
TARGET_TYPES = frozenset({TARGET_POST, TARGET_COMMENT, TARGET_PROFILE})

STATUS_OPEN = "open"
STATUS_DISMISSED = "dismissed"
STATUS_REMOVED = "removed"

#: Why a person says they're reporting something. A short, closed list: an
#: open text box produces "idk it's weird", which tells a moderator nothing
#: and can't be counted.
REPORT_REASONS = {
    "spam": "Spam or scam",
    "nudity": "Nudity or sexual content",
    "hate": "Hate speech or harassment",
    "violence": "Violence or threats",
    "counterfeit": "Counterfeit or misrepresented cards",
    "other": "Something else",
}

#: How much of the offending text is copied onto the case.
EXCERPT_CHARS = 500


async def open_auto_case(
    db: AsyncSession,
    *,
    target_type: str,
    target_id: uuid.UUID,
    author_id: uuid.UUID,
    verdict: moderation.Verdict,
    excerpt: str | None,
) -> None:
    """Record a classifier decision. Caller commits.

    Called for BLOCKED content too: a refusal is exactly the thing a human
    should be able to audit, and it's how we find out the classifier is
    wrong about a word collectors use every day.
    """
    db.add(
        SocialModerationCase(
            target_type=target_type,
            target_id=target_id,
            author_id=author_id,
            source="auto",
            reason=",".join(verdict.categories)[:120] or verdict.action,
            detail=verdict.detail,
            score=verdict.score,
            excerpt=(excerpt or "")[:EXCERPT_CHARS] or None,
            status=STATUS_REMOVED if verdict.blocked else STATUS_OPEN,
            resolved_at=datetime.now(UTC) if verdict.blocked else None,
        )
    )


async def enforce(
    db: AsyncSession,
    *,
    actor: User,
    surface: str,
    target_id: uuid.UUID,
    text: str | None = None,
    images: Sequence[tuple[bytes, str]] | None = None,
    excerpt: str | None = None,
    refusal: str | None = None,
    policy: moderation.Policy | None = None,
) -> moderation.Verdict:
    """**The chokepoint.** Screen one piece of user content, act on the verdict.

    Every path that lets a user publish something another user can see calls
    this and nothing else. That is the whole design: policy sprinkled across
    six call sites is policy that will be implemented five times and
    forgotten once, and the one that gets forgotten is the one that ships a
    hole. Adding a new surface should mean adding a call here, not
    re-deciding what "blocked" means.

    Does three things, in order:

    1. screens the text and/or images in ONE provider call;
    2. **raises 422** if the zero-tolerance set trips — nothing is written,
       and a case is still opened so the refusal is auditable;
    3. opens a review case for anything else doubtful, and returns.

    Callers must invoke this BEFORE they write, and commit afterwards; the
    case row joins whatever transaction they're already in.

    ``policy`` defaults to :data:`moderation.BALANCED`. Pass
    :data:`moderation.IDENTITY` for text that becomes part of someone's
    public identity — a handle or a collection name has no "publish it and
    review later", because it is already on every row the account touches
    by the time a moderator opens the queue.
    """
    verdict = await moderation.screen(
        text, list(images or []), policy=policy or moderation.BALANCED
    )
    if not verdict.needs_review:
        return verdict

    await open_auto_case(
        db,
        target_type=surface,
        target_id=target_id,
        author_id=actor.id,
        verdict=verdict,
        excerpt=excerpt if excerpt is not None else text,
    )
    if verdict.blocked:
        # The case is the record that this was attempted and refused, so it
        # has to survive the caller's rollback — commit it here. The copy
        # comes from ONE registry (moderation.REFUSALS): the backend owns
        # every refusal the user ever reads.
        await db.commit()
        raise HTTPException(
            status_code=422,
            detail=refusal or moderation.refusal_for(surface),
        )
    return verdict


async def report(
    db: AsyncSession, reporter: User, payload: ReportCreate
) -> ModerationCaseRead:
    """A user reports a post, comment or profile."""
    if payload.target_type not in TARGET_TYPES:
        raise HTTPException(status_code=422, detail="Unknown target type")
    if payload.reason not in REPORT_REASONS:
        raise HTTPException(status_code=422, detail="Unknown reason")

    author_id = await _author_of(db, payload.target_type, payload.target_id)
    if author_id is None:
        raise HTTPException(status_code=404, detail="Nothing to report there")
    if author_id == reporter.id:
        raise HTTPException(status_code=400, detail="You can't report your own post")

    # Ids are read into locals BEFORE any write. A failed commit expires
    # every ORM object in the session, so `reporter.id` in the recovery path
    # would try to refresh the row — sync IO on an async session, which
    # raises MissingGreenlet and turns a duplicate report into a 500.
    reporter_id = reporter.id
    mine = select(SocialModerationCase).where(
        SocialModerationCase.target_type == payload.target_type,
        SocialModerationCase.target_id == payload.target_id,
        SocialModerationCase.reporter_id == reporter_id,
    )

    # Check first: the ordinary "I already reported this" path shouldn't
    # need an exception to notice. The constraint stays as the backstop for
    # two taps racing each other.
    existing = (await db.execute(mine)).scalar_one_or_none()
    if existing is not None:
        return _case_read(existing, None, None)

    case = SocialModerationCase(
        target_type=payload.target_type,
        target_id=payload.target_id,
        author_id=author_id,
        source="report",
        reason=payload.reason,
        detail=(payload.note or "").strip()[:1000] or None,
        excerpt=await _excerpt(db, payload.target_type, payload.target_id),
        reporter_id=reporter_id,
        status=STATUS_OPEN,
    )
    db.add(case)
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race — the other insert produced exactly the state this
        # call wanted, so report the winner rather than an error.
        await db.rollback()
        winner = (await db.execute(mine)).scalar_one()
        return _case_read(winner, None, None)
    await db.refresh(case)
    return _case_read(case, None, None)


#: Pseudo-status for the queue filter: content the classifier REFUSED at
#: write time. Born with status=removed and no resolver — no human ever
#: touched it, nothing was ever published.
STATUS_BLOCKED = "blocked"


def _status_conditions(status: str) -> list[Any]:
    """The queue's filter, kept disjoint so every case lives in exactly one
    tab: "blocked" is the machine's refusals, "removed" is a human's."""
    if status == STATUS_BLOCKED:
        return [
            SocialModerationCase.status == STATUS_REMOVED,
            SocialModerationCase.source == "auto",
            SocialModerationCase.resolved_by_id.is_(None),
        ]
    if status == STATUS_REMOVED:
        return [
            SocialModerationCase.status == STATUS_REMOVED,
            SocialModerationCase.resolved_by_id.is_not(None),
        ]
    return [SocialModerationCase.status == status]


async def queue(
    db: AsyncSession,
    *,
    status: str = STATUS_OPEN,
    limit: int = 50,
    offset: int = 0,
) -> ModerationQueueRead:
    """The admin queue. Highest classifier score first, then newest —
    a moderator with ten minutes should spend them on the worst things."""
    conditions = _status_conditions(status)
    base = select(SocialModerationCase).where(*conditions)
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(SocialModerationCase)
                .where(*conditions)
            )
        ).scalar_one()
        or 0
    )
    rows = (
        (
            await db.execute(
                base.order_by(
                    SocialModerationCase.score.desc().nulls_last(),
                    SocialModerationCase.created_at.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    # Handles for the whole page in one query, not one per row.
    user_ids = {c.author_id for c in rows if c.author_id} | {
        c.reporter_id for c in rows if c.reporter_id
    }
    handles: dict[uuid.UUID, str] = {}
    if user_ids:
        handles = {
            row[0]: row[1]
            for row in (
                await db.execute(
                    select(SocialProfile.user_id, SocialProfile.username).where(
                        SocialProfile.user_id.in_(list(user_ids))
                    )
                )
            ).all()
        }

    open_count = (
        total
        if status == STATUS_OPEN
        else int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(SocialModerationCase)
                    .where(SocialModerationCase.status == STATUS_OPEN)
                )
            ).scalar_one()
            or 0
        )
    )
    return ModerationQueueRead(
        items=[
            _case_read(
                case,
                handles.get(case.author_id) if case.author_id else None,
                handles.get(case.reporter_id) if case.reporter_id else None,
            )
            for case in rows
        ],
        total=total,
        open_count=open_count,
    )


async def resolve(
    db: AsyncSession, admin: User, case_id: uuid.UUID, *, action: str
) -> ModerationCaseRead:
    """Close a case. ``action`` is "dismiss" or "remove".

    "remove" soft-deletes a post / hard-deletes a comment. Banning the
    author is deliberately NOT folded in here — it belongs to the user admin
    surface, where it is audit-logged and reversible, and conflating the two
    makes "hide this" one mis-click away from "ban this person".
    """
    case = await db.get(SocialModerationCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    if action not in ("dismiss", "remove"):
        raise HTTPException(status_code=422, detail="Unknown action")

    if action == "remove":
        await _remove_target(db, case.target_type, case.target_id)
        case.status = STATUS_REMOVED
    else:
        case.status = STATUS_DISMISSED
    case.resolved_by_id = admin.id
    case.resolved_at = datetime.now(UTC)

    # Every other open case about the same thing is answered too — leaving
    # nine duplicate reports open after acting on the first is how a queue
    # becomes noise nobody reads.
    siblings = (
        (
            await db.execute(
                select(SocialModerationCase).where(
                    SocialModerationCase.target_type == case.target_type,
                    SocialModerationCase.target_id == case.target_id,
                    SocialModerationCase.status == STATUS_OPEN,
                    SocialModerationCase.id != case.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for sibling in siblings:
        sibling.status = case.status
        sibling.resolved_by_id = admin.id
        sibling.resolved_at = case.resolved_at

    await db.commit()
    await db.refresh(case)
    logger.info(
        "moderation %s case=%s target=%s:%s by=%s",
        action,
        case.id,
        case.target_type,
        case.target_id,
        admin.id,
    )
    return _case_read(case, None, None)


# ── internals ──


async def _author_of(
    db: AsyncSession, target_type: str, target_id: uuid.UUID
) -> uuid.UUID | None:
    if target_type == TARGET_POST:
        post = await db.get(SocialPost, target_id)
        return post.author_id if post and post.deleted_at is None else None
    if target_type == TARGET_COMMENT:
        comment = await db.get(SocialPostComment, target_id)
        return comment.author_id if comment else None
    profile = await db.get(SocialProfile, target_id)
    return profile.user_id if profile else None


async def _excerpt(
    db: AsyncSession, target_type: str, target_id: uuid.UUID
) -> str | None:
    """Copy the text onto the case — the target may be gone before anyone
    reviews it, and "removed something, don't know what" isn't a record."""
    if target_type == TARGET_POST:
        post = await db.get(SocialPost, target_id)
        return (post.body or "")[:EXCERPT_CHARS] if post else None
    if target_type == TARGET_COMMENT:
        comment = await db.get(SocialPostComment, target_id)
        return comment.body[:EXCERPT_CHARS] if comment else None
    profile = await db.get(SocialProfile, target_id)
    return (profile.bio or "")[:EXCERPT_CHARS] if profile else None


async def _remove_target(
    db: AsyncSession, target_type: str, target_id: uuid.UUID
) -> None:
    if target_type == TARGET_POST:
        post = await db.get(SocialPost, target_id)
        if post is not None:
            post.deleted_at = datetime.now(UTC)
    elif target_type == TARGET_COMMENT:
        comment = await db.get(SocialPostComment, target_id)
        if comment is not None:
            await db.delete(comment)
    elif target_type == TARGET_PROFILE:
        # Clear the picture — that IS the actionable part of a profile case,
        # and an avatar rides along on every row the account appears in.
        # Deleting the ACCOUNT is deliberately not folded in here: that's an
        # audit-logged, reversible action over in /admin/users, and putting
        # it one mis-click from "hide this picture" would be reckless. The
        # blob is left in place; the profile stops pointing at it, and the
        # version bump means no client keeps showing the old one.
        profile = await db.get(SocialProfile, target_id)
        if profile is not None:
            profile.avatar_key = None
            profile.avatar_content_type = None
            profile.avatar_version = (profile.avatar_version or 0) + 1
    elif target_type == TARGET_STORY:
        # Soft-delete, the same thing the author's own delete does — every
        # story read filters `deleted_at`, and the row is the record of what
        # was taken down.
        story = await db.get(SocialStory, target_id)
        if story is not None and story.deleted_at is None:
            story.deleted_at = datetime.now(UTC)
    elif target_type == TARGET_STORY_COMMENT:
        story_comment = await db.get(SocialStoryComment, target_id)
        if story_comment is not None and story_comment.deleted_at is None:
            story_comment.deleted_at = datetime.now(UTC)


def _case_read(
    case: SocialModerationCase,
    author_handle: str | None,
    reporter_handle: str | None,
) -> ModerationCaseRead:
    return ModerationCaseRead(
        id=case.id,
        target_type=case.target_type,
        target_id=case.target_id,
        author_id=case.author_id,
        author_username=author_handle,
        source=case.source,
        reason=case.reason,
        reason_label=REPORT_REASONS.get(case.reason or "", case.reason),
        detail=case.detail,
        score=case.score,
        excerpt=case.excerpt,
        reporter_username=reporter_handle,
        status=case.status,
        created_at=case.created_at,
        resolved_at=case.resolved_at,
    )


__all__ = [
    "REPORT_REASONS",
    "STATUS_BLOCKED",
    "STATUS_DISMISSED",
    "STATUS_OPEN",
    "STATUS_REMOVED",
    "TARGET_COLLECTION",
    "TARGET_COMMENT",
    "TARGET_POST",
    "TARGET_PROFILE",
    "TARGET_REVIEW",
    "TARGET_STORY",
    "TARGET_STORY_COMMENT",
    "enforce",
    "open_auto_case",
    "queue",
    "report",
    "resolve",
]
