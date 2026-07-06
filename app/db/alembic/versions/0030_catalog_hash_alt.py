"""Add alternate-art hashes to catalog_image_hashes.

Some catalogs serve two scans of the same card (pokemontcg small vs _hires)
whose pHashes differ by 40+ bits — a scan matching one misses the other.
Store both. Additive — safe online migration.

Revision ID: 0030_catalog_hash_alt
Revises: 0029_catalog_image_hash
Create Date: 2026-07-06 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_catalog_hash_alt"
down_revision: str | Sequence[str] | None = "0029_catalog_image_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "catalog_image_hashes", sa.Column("phash_alt", sa.String(64), nullable=True)
    )
    op.add_column(
        "catalog_image_hashes", sa.Column("dhash_alt", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("catalog_image_hashes", "dhash_alt")
    op.drop_column("catalog_image_hashes", "phash_alt")
