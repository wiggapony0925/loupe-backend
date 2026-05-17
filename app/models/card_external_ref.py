"""External provider references for a canonical :class:`Card`.

A single Loupe card can map to many upstream identifiers — Pokémon TCG,
Scryfall, TCGplayer SKU, PSA spec, eBay EPID, etc.  This table is the spine
of cross-provider lookups: every aggregator (market, comps, listings) should
read external refs here instead of re-resolving by name on every request.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol


class CardExternalRef(Base):
    """Mapping from a Loupe ``Card`` to an upstream provider identifier."""

    __tablename__ = "card_external_refs"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Provider key — e.g. ``pokemontcg``, ``scryfall``, ``ygoprodeck``,
    #: ``tcgplayer``, ``psa``, ``ebay_epid``, ``pricecharting``.
    source: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    #: Provider-native identifier (e.g. ``base1-15``, ``abc-uuid``, ``12345``).
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Optional 0.0-1.0 confidence score from the resolver that created the link.
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_card_external_refs_src_id"),
        Index("ix_card_external_refs_card_source", "card_id", "source"),
    )


__all__ = ["CardExternalRef"]
