"""Card and CardSet ORM models."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import JsonCol, UuidCol
from app.models.enums import TcgEnum


class CardSet(Base):
    """A release set (e.g. Pokémon Base Set, Magic Alpha)."""

    __tablename__ = "card_sets"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    tcg: Mapped[TcgEnum] = mapped_column(
        Enum(TcgEnum, name="tcg_enum"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    release_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    total_cards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cards: Mapped[list[Card]] = relationship("Card", back_populates="card_set")


class Card(Base):
    """A specific printed card within a :class:`CardSet`."""

    __tablename__ = "cards"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    set_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("card_sets.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    tcg: Mapped[TcgEnum] = mapped_column(
        Enum(TcgEnum, name="tcg_enum"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rarity: Mapped[str | None] = mapped_column(String(60), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    card_metadata: Mapped[dict | None] = mapped_column(
        "metadata", JsonCol, nullable=True
    )
    # Perceptual image hashes of the card's reference art, computed by the
    # image-index worker (16x16 imagehash -> 64 hex chars / 256 bits). A
    # live scan's frame hash is matched against these by Hamming distance
    # to identify the card even when OCR is weak. Indexed for prefix scans.
    image_phash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    image_dhash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    card_set: Mapped[CardSet] = relationship("CardSet", back_populates="cards")

    __table_args__ = (Index("ix_cards_tcg_name", "tcg", "name"),)


__all__ = ["Card", "CardSet"]
