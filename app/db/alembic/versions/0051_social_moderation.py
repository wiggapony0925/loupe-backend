"""social moderation — one queue for auto-flags and user reports

Revision ID: 0051_social_moderation
Revises: 0050_social_feed
Create Date: 2026-08-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision = "0051_social_moderation"
down_revision = "0050_social_feed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "social_moderation_cases",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        # No FK: a case points at a post, a comment or a profile.
        sa.Column("target_id", UuidCol(), nullable=False),
        sa.Column(
            "author_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=120), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column(
            "reporter_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="open"
        ),
        sa.Column(
            "resolved_by_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "target_type",
            "target_id",
            "reporter_id",
            name="uq_social_moderation_reporter",
        ),
    )
    op.create_index(
        "ix_social_moderation_status_created",
        "social_moderation_cases",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_social_moderation_target",
        "social_moderation_cases",
        ["target_type", "target_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_social_moderation_target", table_name="social_moderation_cases"
    )
    op.drop_index(
        "ix_social_moderation_status_created", table_name="social_moderation_cases"
    )
    op.drop_table("social_moderation_cases")
