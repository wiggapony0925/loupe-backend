"""store reviews — collectors rate and review physical card shops

Revision ID: 0048_store_reviews
Revises: 0047_blog_content_refresh
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision = "0048_store_reviews"
down_revision = "0047_blog_content_refresh"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "store_reviews",
        sa.Column("id", UuidCol(), primary_key=True),
        # Upstream locator id ("osm:node:123") — stores aren't rows we own.
        sa.Column("store_id", sa.String(length=80), nullable=False),
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("body", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("store_id", "user_id", name="uq_store_review_author"),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_store_review_rating"),
    )
    op.create_index("ix_store_reviews_store_id", "store_reviews", ["store_id"])
    op.create_index("ix_store_reviews_user_id", "store_reviews", ["user_id"])
    op.create_index(
        "ix_store_reviews_store_created", "store_reviews", ["store_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_store_reviews_store_created", table_name="store_reviews")
    op.drop_index("ix_store_reviews_user_id", table_name="store_reviews")
    op.drop_index("ix_store_reviews_store_id", table_name="store_reviews")
    op.drop_table("store_reviews")
