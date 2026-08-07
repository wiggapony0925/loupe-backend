"""saved stores — collectors heart shops into their saved places

Revision ID: 0049_saved_stores
Revises: 0048_store_reviews
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision = "0049_saved_stores"
down_revision = "0048_store_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_stores",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("store_id", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "store_id", name="uq_saved_store_user"),
    )
    op.create_index("ix_saved_stores_user_id", "saved_stores", ["user_id"])
    op.create_index("ix_saved_stores_store_id", "saved_stores", ["store_id"])
    op.create_index(
        "ix_saved_stores_user_created", "saved_stores", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_saved_stores_user_created", table_name="saved_stores")
    op.drop_index("ix_saved_stores_store_id", table_name="saved_stores")
    op.drop_index("ix_saved_stores_user_id", table_name="saved_stores")
    op.drop_table("saved_stores")
