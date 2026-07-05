"""Create catalog_image_hashes — full-catalog perceptual-hash index.

Backfilled over the entire upstream catalog by
``scripts/index_catalog_hashes.py`` so live scans match by artwork alone
(instant, no OCR) for any card. Additive — safe online migration.

Revision ID: 0029_catalog_image_hash
Revises: 0028_push_tokens
Create Date: 2026-07-04 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0029_catalog_image_hash"
down_revision: str | Sequence[str] | None = "0028_push_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_image_hashes",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column("upstream_id", sa.String(128), nullable=False, unique=True),
        sa.Column("tcg", sa.String(32), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("set_name", sa.String(300), nullable=True),
        sa.Column("number", sa.String(64), nullable=True),
        sa.Column("image_url", sa.String(1024), nullable=True),
        sa.Column("phash", sa.String(64), nullable=False),
        sa.Column("dhash", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_catalog_image_hashes_upstream_id",
        "catalog_image_hashes",
        ["upstream_id"],
        unique=True,
    )
    op.create_index("ix_catalog_image_hashes_tcg", "catalog_image_hashes", ["tcg"])
    op.create_index("ix_catalog_image_hashes_phash", "catalog_image_hashes", ["phash"])


def downgrade() -> None:
    op.drop_table("catalog_image_hashes")
