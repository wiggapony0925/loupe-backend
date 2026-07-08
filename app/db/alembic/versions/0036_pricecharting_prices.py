"""Create ``pricecharting_prices`` — local mirror of the PriceCharting guide.

Pre-built storage for the Legendary CSV bulk download. Ships empty and stays
empty on any lower tier, so it has zero effect until a Legendary subscription is
active and a sync runs — then per-card price lookups resolve from here instead
of the 1-request/second API. Pure ``CREATE TABLE`` — safe online, no backfill.

Revision ID: 0036_pricecharting_prices
Revises: 0035_grade_tags
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import JsonCol

revision: str = "0036_pricecharting_prices"
down_revision: str | Sequence[str] | None = "0035_grade_tags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pricecharting_prices",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("product_name", sa.String(length=300), nullable=False),
        sa.Column("product_name_lower", sa.String(length=300), nullable=False),
        sa.Column("console_name", sa.String(length=200), nullable=True),
        sa.Column("console_lower", sa.String(length=200), nullable=True),
        sa.Column("tcg", sa.String(length=32), nullable=True),
        sa.Column("loose_price", sa.Float(), nullable=True),
        sa.Column("ladder", JsonCol, nullable=True),
        sa.Column("sales_volume", sa.Integer(), nullable=True),
        sa.Column("release_date", sa.String(length=20), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_pricecharting_prices_product_name_lower",
        "pricecharting_prices",
        ["product_name_lower"],
    )
    op.create_index(
        "ix_pricecharting_prices_console_lower",
        "pricecharting_prices",
        ["console_lower"],
    )
    op.create_index("ix_pricecharting_prices_tcg", "pricecharting_prices", ["tcg"])


def downgrade() -> None:
    op.drop_index("ix_pricecharting_prices_tcg", table_name="pricecharting_prices")
    op.drop_index(
        "ix_pricecharting_prices_console_lower", table_name="pricecharting_prices"
    )
    op.drop_index(
        "ix_pricecharting_prices_product_name_lower", table_name="pricecharting_prices"
    )
    op.drop_table("pricecharting_prices")
