"""Collection (binder/deck) ORM model + association table."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
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

    __table_args__ = (
        # The composite PK is (collection_id, graded_card_id), so it answers
        # "what is in this binder" and nothing else — a btree cannot seek on
        # its second column alone. Without this index, "which binders hold
        # this card" and the ON DELETE CASCADE fired by removing a card from
        # the vault both read every collection_items row in the database.
        #
        # Migration 0040 created it directly in postgres and no model ever
        # declared it, so any create_all-built database (tests, a fresh local
        # bootstrap) silently lacked it. Declared here so the models are the
        # whole truth; 0056 re-creates it IF NOT EXISTS for databases that
        # already went through 0040.
        Index("ix_collection_items_graded_card_id", "graded_card_id"),
    )


__all__ = ["Collection", "CollectionItem"]
