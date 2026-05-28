"""WatchlistItem ORM model — user-defined "follow this card" pin.

Unlike `PriceAlert` (which fires once when a threshold is crossed), a
watchlist entry is open-ended: the user is just saying "show me this
card in my Watchlist tab and surface notable price moves." One row per
(user, card) pair — uniqueness is enforced at the DB level.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol


class WatchlistItem(Base):
    """A user's pinned card."""

    __tablename__ = "watchlist_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("cards.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "card_id", name="uq_watchlist_user_card"),
        Index("ix_watchlist_user_created", "user_id", "created_at"),
    )


__all__ = ["WatchlistItem"]
