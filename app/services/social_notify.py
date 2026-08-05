"""Community events → inbox + email. The seam the social layer calls.

**Deliberately decoupled from ``app/social``.** Every function takes plain
values (names, usernames, ids) rather than ``SocialProfile`` / ``SocialFollow``
rows, so this module imports nothing from that package and that package needs
nothing from this one beyond a function call. The social slice owns *what
happened*; this owns *how the person hears about it*.

Privacy is the design constraint. A public follow is an accomplished fact
("started following you"); a private one is a pending request that only the
owner can grant. They are separate entry points on purpose — routing both
through one function is exactly how a private account ends up being told
someone already has access.

Every function is best-effort: community notifications must never be able to
fail a follow.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import CATEGORY_SOCIAL
from app.models.user import User, UserSettings
from app.services import email_service, notification_service
from app.services.email_templates.social import (
    build_follow_accepted,
    build_follow_request,
    build_new_follower,
)
from app.utils.logger import get_logger

logger = get_logger("social.notify")


async def _recipient(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """The user, if they're still someone we should be emailing."""
    user = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return user


async def _wants_social_email(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Social mail follows the announcement opt-out.

    A follow notification is somewhere between transactional and marketing: the
    user didn't ask for this specific event, so someone who has switched off
    non-essential mail should not receive it. The in-app notification is
    unconditional either way — muting email shouldn't hide the event entirely.
    """
    row = (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one_or_none()
    return row is None or bool(row.email_announcements_enabled)


async def on_new_follower(
    db: AsyncSession,
    *,
    followee_id: uuid.UUID,
    follower_id: uuid.UUID,
    follower_name: str,
    follower_username: str,
    follower_is_private: bool = False,
    follower_collection_count: int | None = None,
) -> None:
    """Someone started following a public account."""
    try:
        await notification_service.notify(
            db,
            followee_id,
            category=CATEGORY_SOCIAL,
            kind="social_follow",
            title=f"{follower_name or follower_username} started following you",
            body="Tap to see their collection.",
            href=f"/app/u/{follower_username}",
            data={"type": "social_follow", "actorId": str(follower_id)},
            # One notification per (follower, followee) pair. Unfollow and
            # re-follow is a real thing people do; it isn't news twice.
            dedupe_key=f"follow:{follower_id}:{followee_id}",
        )
        user = await _recipient(db, followee_id)
        if user and user.email and await _wants_social_email(db, followee_id):
            content = build_new_follower(
                follower_name=follower_name,
                follower_username=follower_username,
                follower_collection_count=follower_collection_count,
                is_private=follower_is_private,
            )
            await email_service.queue_content(
                user.email, content, category="social", user_id=user.id
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("new-follower notify failed followee=%s (%s)", followee_id, exc)


async def on_follow_request(
    db: AsyncSession,
    *,
    target_id: uuid.UUID,
    requester_id: uuid.UUID,
    requester_name: str,
    requester_username: str,
) -> None:
    """Someone asked to follow a PRIVATE account — nothing is visible yet."""
    try:
        await notification_service.notify(
            db,
            target_id,
            category=CATEGORY_SOCIAL,
            kind="social_follow_request",
            title=f"{requester_name or requester_username} wants to follow you",
            body="Approve or decline the request.",
            href="/app/community/requests",
            data={"type": "social_follow_request", "actorId": str(requester_id)},
            # Re-keyed per request pair. A user who is declined and asks again
            # later should surface again, so the social layer should clear the
            # old row when it deletes the request (see README note).
            dedupe_key=f"follow_req:{requester_id}:{target_id}",
        )
        user = await _recipient(db, target_id)
        if user and user.email and await _wants_social_email(db, target_id):
            content = build_follow_request(
                requester_name=requester_name, requester_username=requester_username
            )
            await email_service.queue_content(
                user.email, content, category="social", user_id=user.id
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("follow-request notify failed target=%s (%s)", target_id, exc)


async def on_follow_accepted(
    db: AsyncSession,
    *,
    requester_id: uuid.UUID,
    owner_id: uuid.UUID,
    owner_name: str,
    owner_username: str,
) -> None:
    """A private account approved a pending request — tell the requester."""
    try:
        await notification_service.notify(
            db,
            requester_id,
            category=CATEGORY_SOCIAL,
            kind="social_follow_accepted",
            title=f"{owner_name or owner_username} accepted your follow request",
            body="Their collection is open to you now.",
            href=f"/app/u/{owner_username}",
            data={"type": "social_follow_accepted", "actorId": str(owner_id)},
            dedupe_key=f"follow_ok:{owner_id}:{requester_id}",
        )
        user = await _recipient(db, requester_id)
        if user and user.email and await _wants_social_email(db, requester_id):
            content = build_follow_accepted(
                owner_name=owner_name, owner_username=owner_username
            )
            await email_service.queue_content(
                user.email, content, category="social", user_id=user.id
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "follow-accepted notify failed requester=%s (%s)", requester_id, exc
        )


async def on_profile_like(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    liker_id: uuid.UUID,
    liker_name: str,
    liker_username: str,
) -> None:
    """Someone liked a profile.

    **In-app only — no email per like, by design.** Likes are the highest-
    volume social event by a wide margin, and one email each is the quickest
    way to train someone to filter you to spam. The email side of likes is a
    periodic digest (``build_profile_likes``) rather than a per-event send.
    """
    try:
        await notification_service.notify(
            db,
            owner_id,
            category=CATEGORY_SOCIAL,
            kind="social_like",
            title=f"{liker_name or liker_username} liked your profile",
            body=None,
            href=f"/app/u/{liker_username}",
            data={"type": "social_like", "actorId": str(liker_id)},
            dedupe_key=f"like:{liker_id}:{owner_id}",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("profile-like notify failed owner=%s (%s)", owner_id, exc)


__all__ = [
    "on_follow_accepted",
    "on_follow_request",
    "on_new_follower",
    "on_profile_like",
]
