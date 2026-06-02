"""Add perceptual-hash columns to ``cards``.

The image-index worker computes a 16×16 pHash/dHash for every catalog
card's reference art. Previously those hashes only lived in the
``cards.metadata`` JSON blob, which the identity resolver couldn't query
efficiently. Promoting them to dedicated, indexed columns lets a live
scan be matched to a catalog card by Hamming distance over the
``image_phash`` column (see
``card_resolver_service.resolve_catalog_by_phash``).

We also backfill the new columns from any hashes already stashed in the
JSON metadata so we don't have to re-download every image.

Revision ID: 0011_card_image_hash
Revises: 0010_watchlist
Create Date: 2026-06-01 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_card_image_hash"
down_revision: str | Sequence[str] | None = "0010_watchlist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cards", sa.Column("image_phash", sa.String(length=64), nullable=True))
    op.add_column("cards", sa.Column("image_dhash", sa.String(length=64), nullable=True))
    op.create_index("ix_cards_image_phash", "cards", ["image_phash"])
    op.create_index("ix_cards_image_dhash", "cards", ["image_dhash"])

    # Backfill from hashes already computed into the metadata JSON blob so
    # existing indexed catalogs become matchable without re-hashing images.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            UPDATE cards
               SET image_phash = metadata -> 'image_hash' ->> 'phash',
                   image_dhash = metadata -> 'image_hash' ->> 'dhash'
             WHERE metadata ? 'image_hash'
            """
        )


def downgrade() -> None:
    op.drop_index("ix_cards_image_dhash", table_name="cards")
    op.drop_index("ix_cards_image_phash", table_name="cards")
    op.drop_column("cards", "image_dhash")
    op.drop_column("cards", "image_phash")
