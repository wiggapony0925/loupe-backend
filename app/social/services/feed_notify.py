"""Feed events → the notification inbox.

One module so the *wording* of every community notification lives together
and stays consistent, and so the feed services never touch the notification
table directly.

Two rules, both learned from the shape of :mod:`app.services.notification_service`:

* **Never notify yourself.** Liking your own post must not light your bell.
* **Never let delivery break the action.** A like that 500s because the
  inbox write failed is a much worse bug than a missing notification, so
  everything here is best-effort and swallows its own errors.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import CATEGORY_SOCIAL
from app.models.user import User
from app.services import notification_service
from app.social.models import SocialPost, SocialPostComment, SocialProfile
from app.utils.logger import get_logger

logger = get_logger("social.feed.notify")


#: Deep link a tapped notification opens. A PATH, not a URL — web and native
#: each resolve it with their own router (see Notification.href).
def post_href(post_id: uuid.UUID) -> str:
    return f"/app/community/p/{post_id}"


def _preview(text: str | None, limit: int = 80) -> str | None:
    """A comment's opening words, for the notification body."""
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _name(profile: SocialProfile, user: User) -> str:
    return user.display_name or f"@{profile.username}"


async def post_liked(db: AsyncSession, *, actor: User, post: SocialPost) -> None:
    """ "@x liked your post"."""
    if post.author_id == actor.id:
        return
    profile = await _profile(db, actor.id)
    if profile is None:
        return
    await _send(
        db,
        post.author_id,
        kind="social_post_like",
        title=f"{_name(profile, actor)} liked your post",
        body=_preview(post.body),
        href=post_href(post.id),
        data={"post_id": str(post.id), "actor_id": str(actor.id)},
        # One notification per (liker, post): unliking and liking again
        # should not be a way to ping someone repeatedly.
        dedupe_key=f"social_post_like:{post.id}:{actor.id}",
    )


async def commented(
    db: AsyncSession,
    *,
    actor: User,
    actor_profile: SocialProfile,
    post: SocialPost,
    comment: SocialPostComment,
    replied_to: SocialPostComment | None,
    mentioned_user_ids: Sequence[uuid.UUID],
) -> None:
    """Tell the post's author, the person replied to, and anyone named.

    Deliberately at most ONE notification per person per comment: replying
    to a comment on your own post while naming yourself should not produce
    three. ``told`` tracks who has already been covered.
    """
    told: set[uuid.UUID] = {actor.id}
    who = _name(actor_profile, actor)
    preview = _preview(comment.body)

    if replied_to is not None and replied_to.author_id not in told:
        told.add(replied_to.author_id)
        await _send(
            db,
            replied_to.author_id,
            kind="social_comment_reply",
            title=f"{who} replied to your comment",
            body=preview,
            href=post_href(post.id),
            data={"post_id": str(post.id), "comment_id": str(comment.id)},
            dedupe_key=f"social_comment:{comment.id}:{replied_to.author_id}",
        )

    if post.author_id not in told:
        told.add(post.author_id)
        await _send(
            db,
            post.author_id,
            kind="social_post_comment",
            title=f"{who} commented on your post",
            body=preview,
            href=post_href(post.id),
            data={"post_id": str(post.id), "comment_id": str(comment.id)},
            dedupe_key=f"social_comment:{comment.id}:{post.author_id}",
        )

    for user_id in mentioned_user_ids:
        if user_id in told:
            continue
        told.add(user_id)
        await _send(
            db,
            user_id,
            kind="social_mention",
            title=f"{who} mentioned you in a comment",
            body=preview,
            href=post_href(post.id),
            data={"post_id": str(post.id), "comment_id": str(comment.id)},
            dedupe_key=f"social_comment:{comment.id}:{user_id}",
        )


async def mentioned_in_post(
    db: AsyncSession,
    *,
    actor: User,
    actor_profile: SocialProfile,
    post: SocialPost,
    user_ids: Sequence[uuid.UUID],
) -> None:
    """ "@x mentioned you in a post"."""
    who = _name(actor_profile, actor)
    for user_id in user_ids:
        if user_id == actor.id:
            continue
        await _send(
            db,
            user_id,
            kind="social_mention",
            title=f"{who} mentioned you in a post",
            body=_preview(post.body),
            href=post_href(post.id),
            data={"post_id": str(post.id), "actor_id": str(actor.id)},
            dedupe_key=f"social_post_mention:{post.id}:{user_id}",
        )


async def followed(db: AsyncSession, *, actor: User, target_id: uuid.UUID) -> None:
    """ "@x started following you" — the bell's most common entry."""
    if target_id == actor.id:
        return
    profile = await _profile(db, actor.id)
    if profile is None:
        return
    await _send(
        db,
        target_id,
        kind="social_follow",
        title=f"{_name(profile, actor)} started following you",
        body=None,
        href=f"/app/u/{profile.username}",
        data={"actor_id": str(actor.id), "username": profile.username},
        # Per (follower, followee) forever: unfollow/refollow cycling is not
        # a notification channel.
        dedupe_key=f"social_follow:{actor.id}:{target_id}",
    )


async def _profile(db: AsyncSession, user_id: uuid.UUID) -> SocialProfile | None:
    return (
        await db.execute(select(SocialProfile).where(SocialProfile.user_id == user_id))
    ).scalar_one_or_none()


async def _send(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    kind: str,
    title: str,
    body: str | None,
    href: str,
    data: dict[str, Any],
    dedupe_key: str,
) -> None:
    """Best-effort delivery — a failed notification never fails the action."""
    try:
        await notification_service.notify(
            db,
            user_id,
            category=CATEGORY_SOCIAL,
            kind=kind,
            title=title,
            body=body,
            href=href,
            data=data,
            dedupe_key=dedupe_key,
        )
    except Exception:
        logger.exception("social notification failed user=%s", user_id)
        await db.rollback()


__all__ = [
    "commented",
    "followed",
    "mentioned_in_post",
    "post_href",
    "post_liked",
]
