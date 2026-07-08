"""Local catalog mirror — our own durable copy of an upstream card catalog.

Why this exists: the public catalog surfaces (browse / search / card detail)
used to live-proxy pokemontcg.io on every request. That upstream is
intermittently slow or degraded (200s with empty ``data``), which surfaced to
users as 30s+ skeletons and "missing" cards. The mirror stores the complete
catalog in Postgres — durable, shared across instances, immune to upstream
blips — so catalog reads are single-digit-millisecond DB queries and the
upstream is only consulted by background syncs and price refreshes.

``payload`` keeps the card JSON in the exact shape the upstream API returns
(with the ``set`` object injected), so the existing ``_from_pokemon``
converter renders mirror rows identically to live responses.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JsonCol


class CatalogMirrorSet(Base):
    """One upstream set (e.g. Pokémon ``me4`` / Chaos Rising)."""

    __tablename__ = "catalog_mirror_sets"

    #: Provider-local set id, e.g. ``me4``.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tcg: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    series: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Upstream date string ``YYYY/MM/DD`` — sorts correctly lexically.
    release_date: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    printed_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Raw upstream set object (API shape, incl. images).
    payload: Mapped[dict | None] = mapped_column(JsonCol, nullable=True)
    #: Rows currently in ``catalog_mirror_cards`` for this set.
    card_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Last time embedded tcgplayer/cardmarket prices were refreshed from the
    #: live API. Null until the first successful price pass.
    prices_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CatalogMirrorCard(Base):
    """One card printing, stored in upstream API shape."""

    __tablename__ = "catalog_mirror_cards"

    #: Composite unified id, e.g. ``pokemontcg:me4-1`` — same id the public
    #: API contract already uses, so mirror rows are drop-in.
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tcg: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: Provider-local id, e.g. ``me4-1``.
    upstream_id: Mapped[str] = mapped_column(String(96), nullable=False)
    set_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    set_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Lower-cased name for cross-dialect case-insensitive search/sort.
    name_lower: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: Collector number reduced to bare digits ("058/102" → "58") — the
    #: identification pipeline pins on this.
    bare_number: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    #: Numeric prefix of the collector number for stable in-set ordering.
    number_int: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rarity: Mapped[str | None] = mapped_column(String(60), nullable=True)
    #: ISO language of this printing (en, fr, de, ja…). Existing rows backfill
    #: to "en"; the client language picker + the search `langs` filter key off
    #: this so a user can mix languages or stay on English (the default).
    language: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="en", index=True
    )
    #: Denormalized from the set for "newest" sorts without a join.
    release_date: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    #: Best embedded market price (USD-ish) for price sorts; null = unpriced.
    sort_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Full upstream-shaped card JSON (``set`` object injected).
    payload: Mapped[dict] = mapped_column(JsonCol, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Browse-by-set pages order on (set, collector number).
        Index("ix_catalog_mirror_cards_set_order", "set_id", "number_int"),
        # "Newest" full-catalog browse.
        Index("ix_catalog_mirror_cards_newest", "tcg", "release_date"),
        # Price sorts (nulls excluded in the query).
        Index("ix_catalog_mirror_cards_price", "tcg", "sort_price"),
    )


__all__ = ["CatalogMirrorCard", "CatalogMirrorSet"]
