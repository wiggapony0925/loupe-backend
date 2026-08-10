"""Every notification Loupe sends, declared once.

This is the catalog the user-facing inbox AND the push that lands on the
phone both render from. One template produces one set of strings — title,
body, deep link — handed to :func:`app.services.notification_service.notify`,
which writes the inbox row and forwards the SAME strings to the push
transport. The two legs cannot drift because they are the same render.

Why a registry and not copy at the call sites: the wording lived in three
places before this (``app/social/services/feed_notify.py``, a dead
``app/services/social_notify.py`` twin with different dedupe keys for the
same events, and inline f-strings in the price-alert job). Two modules
composing "started following you" with different keys is how one follow
becomes two notifications.

What belongs here: every notification a PRODUCT EVENT produces. What does
not: operator-composed sends (``admin_direct``, ``admin_broadcast``,
``admin_test``) and article announcements (``blog_post``), whose copy IS
the operator's/article's own words — they go straight through
``notification_service`` and are listed here only in this docstring so the
catalog stays complete.

Community scoping is a design rule, not an accident: nothing in this
registry fans out beyond the follow graph and direct interactions —
``social_new_post`` goes to the author's FOLLOWERS only (see
``feed_notify.posted``), and everything else goes to exactly the person
acted upon (liked, replied, mentioned, followed, requested).

Rendering rules:

* ``title``/``href``/``dedupe`` are ``str.format`` templates over the
  params the composer passes (plus ``recipient_id``, always available).
  A missing param is a caller bug and raises — tests catch it.
* ``body`` is optional two ways: a template of ``None`` means "this kind
  has no body", and a body whose params include a ``None`` (a post with no
  caption) drops to no body instead of printing ``"None"``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from string import Formatter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    CATEGORY_MARKET,
    CATEGORY_SOCIAL,
    Notification,
)
from app.services import notification_service


@dataclass(frozen=True, slots=True)
class NotificationTemplate:
    """One kind of notification: category, copy, link, dedupe, push policy."""

    #: Wire kind — what clients switch icons on. Several templates may share
    #: one kind (a mention in a post vs. in a comment) without sharing copy.
    kind: str
    category: str
    title: str
    body: str | None = None
    href: str | None = None
    dedupe: str | None = None
    #: Whether this kind is worth a buzz on the phone. The inbox row is
    #: written either way.
    push: bool = True


#: Registry key → template. Keys are COMPOSER-facing ids; the wire ``kind``
#: is a field, so two entries can share a kind with different copy/dedupe.
TEMPLATES: dict[str, NotificationTemplate] = {
    # ── Community: the feed ──
    "social_new_post": NotificationTemplate(
        kind="social_new_post",
        category=CATEGORY_SOCIAL,
        title="{actor} posted",
        body="{summary}",
        href="/app/community/p/{post_id}",
        # One per (post, recipient) — a retry or a re-run can't double up.
        dedupe="social_new_post:{post_id}:{recipient_id}",
    ),
    "social_post_like": NotificationTemplate(
        kind="social_post_like",
        category=CATEGORY_SOCIAL,
        title="{actor} liked your post",
        body="{preview}",
        href="/app/community/p/{post_id}",
        # One per (liker, post): unliking and liking again should not be a
        # way to ping someone repeatedly.
        dedupe="social_post_like:{post_id}:{actor_id}",
    ),
    "social_post_comment": NotificationTemplate(
        kind="social_post_comment",
        category=CATEGORY_SOCIAL,
        title="{actor} commented on your post",
        body="{preview}",
        href="/app/community/p/{post_id}",
        dedupe="social_comment:{comment_id}:{recipient_id}",
    ),
    "social_comment_reply": NotificationTemplate(
        kind="social_comment_reply",
        category=CATEGORY_SOCIAL,
        title="{actor} replied to your comment",
        body="{preview}",
        href="/app/community/p/{post_id}",
        dedupe="social_comment:{comment_id}:{recipient_id}",
    ),
    # One wire kind, two surfaces — the dedupe keys deliberately match the
    # historical ones so an OTA'd backend can't re-notify old events.
    "social_mention_post": NotificationTemplate(
        kind="social_mention",
        category=CATEGORY_SOCIAL,
        title="{actor} mentioned you in a post",
        body="{preview}",
        href="/app/community/p/{post_id}",
        dedupe="social_post_mention:{post_id}:{recipient_id}",
    ),
    "social_mention_comment": NotificationTemplate(
        kind="social_mention",
        category=CATEGORY_SOCIAL,
        title="{actor} mentioned you in a comment",
        body="{preview}",
        href="/app/community/p/{post_id}",
        dedupe="social_comment:{comment_id}:{recipient_id}",
    ),
    # ── Community: the graph ──
    "social_follow": NotificationTemplate(
        kind="social_follow",
        category=CATEGORY_SOCIAL,
        title="{actor} started following you",
        href="/app/u/{actor_username}",
        # Per (follower, followee) forever: unfollow/refollow cycling is
        # not a notification channel.
        dedupe="social_follow:{actor_id}:{recipient_id}",
    ),
    "social_follow_request": NotificationTemplate(
        kind="social_follow_request",
        category=CATEGORY_SOCIAL,
        title="{actor} wants to follow you",
        body="Approve or decline the request.",
        href="/app/community/requests",
        dedupe="follow_req:{actor_id}:{recipient_id}",
    ),
    "social_follow_accepted": NotificationTemplate(
        kind="social_follow_accepted",
        category=CATEGORY_SOCIAL,
        title="{actor} accepted your follow request",
        body="Their collection is open to you now.",
        href="/app/u/{actor_username}",
        dedupe="follow_ok:{actor_id}:{recipient_id}",
    ),
    # ── Market ──
    "price_alert": NotificationTemplate(
        kind="price_alert",
        category=CATEGORY_MARKET,
        title="{arrow} {card_name} — {price}",
        body="Just {moved} your {threshold} alert.",
        href="/cards/{card_id}",
        # Keyed on the alert row: a re-run of the job can't post the same
        # fire twice.
        dedupe="alert:{alert_ref}:{recipient_id}",
    ),
}


@dataclass(frozen=True, slots=True)
class RenderedNotification:
    kind: str
    category: str
    title: str
    body: str | None
    href: str | None
    dedupe_key: str | None
    push: bool


def _fields(template: str) -> list[str]:
    return [f for _, f, _, _ in Formatter().parse(template) if f]


def render(template_id: str, params: dict[str, Any]) -> RenderedNotification:
    """Resolve one template against its params.

    Raises ``KeyError`` when the title/href/dedupe reference a param the
    composer didn't pass — that's a programming error, not data.
    """
    t = TEMPLATES[template_id]
    body: str | None = None
    if t.body is not None and all(params.get(f) is not None for f in _fields(t.body)):
        body = t.body.format(**params)
    return RenderedNotification(
        kind=t.kind,
        category=t.category,
        title=t.title.format(**params),
        body=body,
        href=t.href.format(**params) if t.href else None,
        dedupe_key=t.dedupe.format(**params) if t.dedupe else None,
        push=t.push,
    )


async def send(
    db: AsyncSession,
    recipient_id: uuid.UUID,
    template_id: str,
    *,
    data: dict[str, Any] | None = None,
    image_url: str | None = None,
    **params: Any,
) -> Notification | None:
    """Render + deliver: one inbox row, and the same strings to the phone."""
    rendered = render(template_id, {**params, "recipient_id": recipient_id})
    return await notification_service.notify(
        db,
        recipient_id,
        category=rendered.category,
        kind=rendered.kind,
        title=rendered.title,
        body=rendered.body,
        href=rendered.href,
        image_url=image_url,
        data=data,
        dedupe_key=rendered.dedupe_key,
        push=rendered.push,
    )


__all__ = [
    "TEMPLATES",
    "NotificationTemplate",
    "RenderedNotification",
    "render",
    "send",
]
