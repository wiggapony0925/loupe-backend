"""Create ``price_alerts`` table.

Users can subscribe to "ping me when card X crosses $Y" alerts. The
worker checks `triggered_at IS NULL` rows whenever a fresh price lands
and sets the column when the condition first fires.

Revision ID: 0005_price_alerts
Revises: 0004_grade_cost_basis
Create Date: 2026-05-19 09:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0005_price_alerts"
down_revision: str | Sequence[str] | None = "0004_grade_cost_basis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_condition = sa.Enum("above", "below", name="price_alert_condition")
# Column-level reference that must NOT try to recreate the type — we pre-create
# it once with `checkfirst=True` below. Without `create_type=False`, alembic
# emits a second `CREATE TYPE` inside `create_table` which fails on re-runs.
_condition_col = sa.Enum(
    "above", "below", name="price_alert_condition", create_type=False
)


def upgrade() -> None:
    op.create_table(
        "price_alerts",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "card_id",
            UuidCol(),
            sa.ForeignKey("cards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("condition", _condition_col, nullable=False),
        sa.Column("threshold_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("note", sa.String(length=280), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_price_usd", sa.Numeric(10, 2), nullable=True),
    )
    op.create_index(
        "ix_price_alerts_user_id", "price_alerts", ["user_id"]
    )
    op.create_index(
        "ix_price_alerts_card_id", "price_alerts", ["card_id"]
    )
    op.create_index(
        "ix_price_alerts_user_card", "price_alerts", ["user_id", "card_id"]
    )
    op.create_index(
        "ix_price_alerts_active", "price_alerts", ["card_id", "triggered_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_price_alerts_active", table_name="price_alerts")
    op.drop_index("ix_price_alerts_user_card", table_name="price_alerts")
    op.drop_index("ix_price_alerts_card_id", table_name="price_alerts")
    op.drop_index("ix_price_alerts_user_id", table_name="price_alerts")
    op.drop_table("price_alerts")
    _condition.drop(op.get_bind(), checkfirst=True)
