"""Create ``watchlist_items`` table.

Users can pin cards to a personal "watchlist" surface — same idea as
GitHub stars or eBay watched listings. One row per (user, card) pair,
enforced at the DB layer so the API's idempotent ``POST /watchlist``
can rely on the unique constraint as the race-safety mechanism.

Revision ID: 0010_watchlist
Revises: 0009_card_identifications
Create Date: 2026-05-28 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0010_watchlist"
down_revision: str | Sequence[str] | None = "0009_card_identifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watchlist_items",
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
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id", "card_id", name="uq_watchlist_user_card"
        ),
    )
    op.create_index(
        "ix_watchlist_items_user_id", "watchlist_items", ["user_id"]
    )
    op.create_index(
        "ix_watchlist_items_card_id", "watchlist_items", ["card_id"]
    )
    op.create_index(
        "ix_watchlist_user_created",
        "watchlist_items",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_watchlist_user_created", table_name="watchlist_items")
    op.drop_index("ix_watchlist_items_card_id", table_name="watchlist_items")
    op.drop_index("ix_watchlist_items_user_id", table_name="watchlist_items")
    op.drop_table("watchlist_items")
