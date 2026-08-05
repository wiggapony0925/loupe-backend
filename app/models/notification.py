"""Notification — one delivered thing, recorded server-side.

Before this table the inbox was *synthesized on the device*: the app fetched
the announcement, the blog list and the user's price alerts, then merged them
into a feed. That made three things impossible — read state died with the
install (it lived in device storage), nothing could be paginated (there was no
list, only a merge), and anything the backend wanted to tell one specific user
had nowhere to live. It also meant the inbox showed whatever happened to be in
those endpoints rather than what was actually sent.

A row here is a fact: *this user was told this thing at this time*. Push is a
delivery channel for that fact, not the fact itself — so a notification still
exists (and still counts as unread) when the device has no token, has push
switched off, or was offline when it fired.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol

#: Broad grouping the clients filter and tint by. Kept small on purpose — a
#: category is a lens the user chooses, not a taxonomy of every event.
CATEGORY_MARKET = "market"  # price alerts, movers
CATEGORY_NEWS = "news"  # articles, product announcements
CATEGORY_SOCIAL = "social"  # follows, likes, requests
CATEGORY_BILLING = "billing"  # payment + plan changes
CATEGORY_SYSTEM = "system"  # security, account, everything else

CATEGORIES = frozenset(
    {
        CATEGORY_MARKET,
        CATEGORY_NEWS,
        CATEGORY_SOCIAL,
        CATEGORY_BILLING,
        CATEGORY_SYSTEM,
    }
)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    #: One of :data:`CATEGORIES`.
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The specific event ("price_alert", "blog_post", "social_follow", …).
    #: Clients switch icons on this; analytics groups by it.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: In-app route to open on tap (``/cards/123``). Deliberately a path, not a
    #: URL: web and mobile resolve it against their own navigators.
    href: Mapped[str | None] = mapped_column(String(500), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    #: Structured extras the clients may render (card id, actor id, amounts).
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    #: Natural key of the underlying event ("blog:<post_id>", "follow:<a>:<b>").
    #: Unique, so a replayed webhook, a re-run cron, or two racing workers
    #: can't post the same thing twice. NULL for genuinely repeatable events.
    dedupe_key: Mapped[str | None] = mapped_column(
        String(200), unique=True, nullable=True
    )

    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When the push was handed to Expo — NULL means never pushed (no token,
    #: push disabled, or delivery failed). Distinct from `read_at`: a
    #: notification can be read in-app having never reached a device.
    pushed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (
        # The inbox query: this user's newest first. Composite so the page
        # never degrades into a per-user scan + sort as the table grows.
        Index("ix_notifications_user_created", "user_id", "created_at"),
        # The badge query: unread for this user. Postgres can answer the count
        # from this index alone.
        Index("ix_notifications_user_read", "user_id", "read_at"),
    )


__all__ = [
    "CATEGORIES",
    "CATEGORY_BILLING",
    "CATEGORY_MARKET",
    "CATEGORY_NEWS",
    "CATEGORY_SOCIAL",
    "CATEGORY_SYSTEM",
    "Notification",
]
