"""Add ``user_recents`` for cross-device recent searches + recently-viewed.

One row per user, two small JSON lists. Additive + nullable — safe online
migration, no backfill.

Revision ID: 0020_user_recents
Revises: 0019_site_config
Create Date: 2026-06-22 22:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0020_user_recents"
down_revision: str | Sequence[str] | None = "0019_site_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_recents",
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("searches", sa.JSON(), nullable=True),
        sa.Column("viewed", sa.JSON(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("user_recents")
