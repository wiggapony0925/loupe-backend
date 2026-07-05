"""CatalogImageHash — a full-catalog perceptual-hash index for scanning.

One row per catalog card (across every game), keyed by its composite
``upstream_id`` (e.g. ``pokemontcg:base1-4``). The row stores the card's
perceptual hashes (``phash``/``dhash`` of the reference art) plus just enough
denormalized identity (name / set / number / image) to return a scan candidate
without a second upstream fetch.

Unlike ``Card.image_phash`` — which only covers the sliver of cards users have
materialized locally — this table is backfilled over the ENTIRE upstream
catalog by ``scripts/index_catalog_hashes.py``, so a live scan can be matched
by artwork alone (instant, no OCR) for any card in the world. The matcher lives
in ``catalog_hash_index.py`` (an in-memory Hamming index refreshed from here).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol


class CatalogImageHash(Base):
    __tablename__ = "catalog_image_hashes"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    #: Composite catalog id — ``<source>:<external_id>``. The join key back to
    #: the upstream catalog; unique so the indexer upserts idempotently.
    upstream_id: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    #: Game — ``pokemon`` | ``magic`` | ``yugioh`` | … — lets the matcher scope
    #: to the user's selected TCG for a smaller, faster candidate set.
    tcg: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: Denormalized identity so a hash hit returns a candidate with no refetch.
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    set_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: 16x16 imagehash → 64 hex chars / 256 bits. The first 4 hex are indexed
    #: for a cheap prefix pre-filter should the in-memory cache ever be bypassed.
    phash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dhash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


__all__ = ["CatalogImageHash"]
