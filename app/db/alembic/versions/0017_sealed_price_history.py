"""Add ``price_history`` to ``sealed_products``.

Sealed SKUs have no stored price history (unlike cards, which accumulate
``price_snapshots``). This adds a small JSON column we append a daily
market observation to — ``[{"d": "2026-06-22", "p": 304.54}, …]`` — so the
detail page's value line fills into a real multi-point curve over time.
Nullable + additive: safe online migration, no backfill required.

Revision ID: 0017_sealed_price_history
Revises: 0016_seed_portal_content
Create Date: 2026-06-22 16:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_sealed_price_history"
down_revision: str | Sequence[str] | None = "0016_seed_portal_content"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sealed_products",
        sa.Column("price_history", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sealed_products", "price_history")
