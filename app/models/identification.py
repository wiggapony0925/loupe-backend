"""ORM models for the card-identification analytics + feedback loop.

Two tables:

* :class:`CardIdentification` — one row per ``POST /v1/cards/identify``
  call. Stores enough signal to compute accuracy + cost retrospectively
  and to fuel the feedback re-ranker.
* :class:`IdentificationFeedback` — one row per user thumbs-up / down
  (and optional "actually it was *this* card" correction).

Both tables intentionally avoid foreign keys to the catalog ``cards``
table — the user may identify a card that hasn't been materialised
locally yet (the row is upstream-only at the time of the scan). We store
the composite ``upstream_id`` string and resolve later via
``card_resolver_service`` when needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JsonCol, UuidCol


class CardIdentification(Base):
    """One ``POST /v1/cards/identify`` invocation."""

    __tablename__ = "card_identifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    # Nullable so anonymous (pre-login) scans can still flow through the
    # pipeline; we just can't attribute their feedback to a profile.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    # SHA-256 of the *input* image. Acts as a dedup key for the eval
    # harness (re-running the same fixture must not double-bill Vision).
    image_sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # pHash of the image — duplicated from the fingerprint table for
    # convenience so analytics queries don't need a join.
    phash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Which OCR provider produced the text (``mock`` | ``google_vision``).
    # Persisted so accuracy comparisons across providers are possible.
    ocr_provider: Mapped[str] = mapped_column(String(40), nullable=False)
    # Raw OCR text, truncated. Kept verbatim so we can re-rank later as
    # parsing logic improves. Cap at 8 KB to bound row size.
    ocr_full_text: Mapped[str] = mapped_column(String(8000), nullable=False, default="")
    ocr_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Parsed signals from the OCR text. Indexed by ``parsed_title`` because
    # the feedback prior query filters on it.
    parsed_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parsed_set_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    parsed_card_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tcg_inferred: Mapped[str] = mapped_column(String(16), nullable=False)
    primary_source: Mapped[str] = mapped_column(String(16), nullable=False)
    # Top candidate (denormalized for fast admin metrics queries; the full
    # ranked list lives in ``candidates_json``).
    top_card_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    top_upstream_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    top_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # JSON array of {card_id, upstream_id, name, confidence, source, breakdown}.
    candidates_json: Mapped[list | None] = mapped_column(JsonCol, nullable=True)
    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_card_identifications_parsed_title_lower",
            func.lower(parsed_title),
        ),
        Index("ix_card_identifications_created_at", "created_at"),
        Index("ix_card_identifications_tcg_provider", "tcg_inferred", "ocr_provider"),
    )


class IdentificationFeedback(Base):
    """User confirmation (or correction) of a prior identification."""

    __tablename__ = "identification_feedback"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    identification_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("card_identifications.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Either a local UUID (when the user picked a materialized card) or
    # a composite ``upstream_id`` string. Stored as TEXT so both fit.
    chosen_card_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["CardIdentification", "IdentificationFeedback"]
