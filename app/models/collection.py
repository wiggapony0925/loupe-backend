"""Collection (binder/deck) ORM model + association table."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol


class Collection(Base):
    """A user-curated binder/deck of graded cards."""

    __tablename__ = "collections"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_public: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CollectionItem(Base):
    """Association row between :class:`Collection` and a graded card."""

    __tablename__ = "collection_items"

    collection_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    graded_card_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("graded_cards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["Collection", "CollectionItem"]
