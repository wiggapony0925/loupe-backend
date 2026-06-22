"""Sealed-product ORM models — catalog + per-user ownership.

Sealed TCG product (booster boxes, ETBs, tins, etc.) trades like a card:
collectors buy modern sets sealed and watch them appreciate over years.
Tracking sealed in the same app as singles is table-stakes if we're
serious about beating Collectr; this module is the catalog half.

Two tables:

* :class:`SealedProduct` — the catalog row. One per SKU regardless of
  how many users own it. Carries TCG, product type, image, MSRP, and
  release metadata so the search UI can group / filter without an
  N+1 fetch.
* :class:`SealedHolding` — the user's ownership row. Quantity-based
  (a single row covers "I own 6 of these") plus cost-basis fields
  that mirror :class:`~app.models.grade.GradedCard` so portfolio
  rollups can compose without a translation layer.

Both have soft-delete via ``deleted_at`` and shared timestamps so the
update/delete endpoints look identical to the graded-card surface.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import JsonCol, UuidCol
from app.models.enums import SealedProductTypeEnum, TcgEnum


class SealedProduct(Base):
    """A specific sealed SKU (e.g. "Pokémon Scarlet & Violet 151 ETB")."""

    __tablename__ = "sealed_products"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    tcg: Mapped[TcgEnum] = mapped_column(
        Enum(TcgEnum, name="tcg_enum"), nullable=False, index=True
    )
    product_type: Mapped[SealedProductTypeEnum] = mapped_column(
        Enum(SealedProductTypeEnum, name="sealed_product_type_enum"),
        nullable=False,
        index=True,
    )
    set_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(),
        ForeignKey("card_sets.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    # Display name — what we render in search results. Set + product type
    # are usually embedded ("Scarlet & Violet 151 Elite Trainer Box") so
    # collectors can recognise a SKU even without the set join.
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    # The set name as printed on the box, denormalised so search works
    # even before the set_id join lands. Nullable because some sealed
    # (organized-play kits, sleeve bundles) aren't tied to a single set.
    set_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    msrp_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    release_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    # Accumulated daily market observations: [{"d": ISO-date, "p": price}, …].
    # Appended best-effort on each market resolve so the value line grows into
    # a real multi-point curve over time.
    price_history: Mapped[list | None] = mapped_column(JsonCol, nullable=True)
    # Source + upstream id (e.g. "tcgplayer:1234567") so future ingestion
    # passes can dedupe without re-creating the catalog row. Unique
    # together via the table constraint below.
    upstream_source: Mapped[str | None] = mapped_column(
        String(40), nullable=True, index=True
    )
    upstream_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    holdings: Mapped[list[SealedHolding]] = relationship(
        "SealedHolding", back_populates="product"
    )

    __table_args__ = (
        Index("ix_sealed_products_tcg_type", "tcg", "product_type"),
        Index("ix_sealed_products_name_lower", func.lower(name)),
        # One upstream row per (source, id). Lets the seed script + future
        # ingestion stay idempotent without a SELECT-then-INSERT race.
        UniqueConstraint(
            "upstream_source",
            "upstream_id",
            name="uq_sealed_products_upstream",
        ),
    )


class SealedHolding(Base):
    """A user's ownership of one sealed SKU (quantity-based)."""

    __tablename__ = "sealed_holdings"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("sealed_products.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    # Quantity-based — a single row covers "I own 6 of these" so the
    # vault doesn't blow up when collectors stack cases. Defaults to 1.
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    purchase_price_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    purchase_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    # Current market estimate (USD) — user-entered until we wire the
    # comps pipeline to sealed SKUs.
    estimated_value_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Set once the user "opens" the box. Lets us hide opened sealed from
    # the investment-rollup while still preserving the purchase record
    # (e.g. "I paid \$120 for this ETB before I ripped it").
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
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
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    product: Mapped[SealedProduct] = relationship(
        "SealedProduct", back_populates="holdings"
    )

    __table_args__ = (
        Index("ix_sealed_holdings_user_acquired", "user_id", "acquired_at"),
    )


__all__ = ["SealedHolding", "SealedProduct"]
