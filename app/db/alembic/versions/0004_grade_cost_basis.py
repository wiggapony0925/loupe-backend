"""Add cost-basis columns to ``graded_cards``.

`purchase_price_usd` and `purchase_date` capture what the collector paid
and when, enabling real P/L on the portfolio chart (current value minus
cost basis). Both columns are nullable so existing rows and scanner-only
rows stay valid; the UI treats `null` as "no cost recorded" instead of
zero.

Revision ID: 0004_grade_cost_basis
Revises: 0003_card_external_refs
Create Date: 2026-05-19 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_grade_cost_basis"
down_revision: str | Sequence[str] | None = "0003_card_external_refs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "graded_cards",
        sa.Column("purchase_price_usd", sa.Numeric(10, 2), nullable=True),
    )
    op.add_column(
        "graded_cards",
        sa.Column("purchase_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("graded_cards", "purchase_date")
    op.drop_column("graded_cards", "purchase_price_usd")
