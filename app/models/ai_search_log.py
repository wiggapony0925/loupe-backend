"""AiSearchLog — one row per Loupe AI "describe it" ask.

The chatbot's flight recorder: what was asked, by whom, what the model
answered (message + candidates), how it was served (fresh model call vs
cached plan vs plain-search fallback), how fast, and — once the user taps
thumbs up/down under the answer — whether it was actually RIGHT. Powers the
/admin/ai dev-portal conversations view and the accuracy analytics.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol

#: How the answer was produced, for :attr:`AiSearchLog.source`.
AI_SEARCH_SOURCES = ("ai", "fallback")

#: Feedback verdicts: +1 thumbs up, -1 thumbs down, NULL = not rated.
FEEDBACK_UP = 1
FEEDBACK_DOWN = -1


class AiSearchLog(Base):
    __tablename__ = "ai_search_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    # Best-effort link to the asker (SET NULL if the account is deleted).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    query: Mapped[str] = mapped_column(String(220), nullable=False)
    # The game tag active in the search UI when they asked.
    game_hint: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # The game the answer was scoped to (model's call, else the hint).
    game: Mapped[str | None] = mapped_column(String(20), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(12), index=True, nullable=False)
    # True when the plan came from kv_cache (no model call was made).
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    candidates: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # +1 thumbs up / -1 thumbs down, set by the asker under the answer bubble.
    feedback: Mapped[int | None] = mapped_column(
        SmallInteger, index=True, nullable=True
    )
    feedback_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


__all__ = ["AI_SEARCH_SOURCES", "FEEDBACK_DOWN", "FEEDBACK_UP", "AiSearchLog"]
