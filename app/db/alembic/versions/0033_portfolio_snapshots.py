"""portfolio_snapshots — point-in-time canonical portfolio totals.

Backs the 1D portfolio chart with REAL intraday observations: the backend
captures the live grade-aware total opportunistically (throttled,
write-on-read) and the history endpoint splices these points between the
yesterday-close and live-now anchors. Bounded: 7-day retention enforced
on the write path.

Revision ID: 0033_portfolio_snapshots
Revises: 0032_currency_code_width
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_portfolio_snapshots"
down_revision: str | Sequence[str] | None = "0032_currency_code_width"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_value_usd", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "holdings_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_index(
        "ix_portfolio_snapshots_user_captured",
        "portfolio_snapshots",
        ["user_id", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_snapshots_user_captured", table_name="portfolio_snapshots"
    )
    op.drop_table("portfolio_snapshots")
