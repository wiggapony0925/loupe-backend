"""Per-user recent searches + recently-viewed, for cross-device sync.

A single row per user holding two small JSON lists: the last search queries
and the last cards/sealed products the user opened. Clients (web + mobile)
keep a device-local copy and reconcile against this on sign-in, so a user's
recents follow them between devices. Capped + deduped client-side; the server
just stores whatever the client sends.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JsonCol, UuidCol


class UserRecents(Base):
    """One row per user: recent search queries + recently-viewed items."""

    __tablename__ = "user_recents"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: List[str] — most-recent-first search queries.
    searches: Mapped[list | None] = mapped_column(JsonCol, nullable=True)
    #: List[{id, name, imageUrl, setName, kind}] — most-recent-first views.
    viewed: Mapped[list | None] = mapped_column(JsonCol, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
