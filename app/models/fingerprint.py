"""Fingerprint ORM model — perceptual hash + feature vector for a graded card."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JsonCol, UuidCol


class Fingerprint(Base):
    """Perceptual hashes + feature vector used to detect duplicate grades."""

    __tablename__ = "fingerprints"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    graded_card_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("graded_cards.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    phash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    dhash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    feature_vector: Mapped[dict | None] = mapped_column(JsonCol, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["Fingerprint"]
