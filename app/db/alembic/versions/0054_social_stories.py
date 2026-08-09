"""Stories: 24-hour photo/video posts, their views and their comments.

Revision ID: 0054_social_stories
Revises: 0053_post_edited_at
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision = "0054_social_stories"
down_revision = "0053_post_edited_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "social_stories",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "author_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Expiry is a column, not a job — see the model. Indexed because
        # every read filters on it.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_social_stories_author_id", "social_stories", ["author_id"])
    op.create_index("ix_social_stories_expires_at", "social_stories", ["expires_at"])
    op.create_index(
        "ix_social_stories_author_expires",
        "social_stories",
        ["author_id", "expires_at"],
    )

    op.create_table(
        "social_story_views",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "story_id",
            UuidCol(),
            sa.ForeignKey("social_stories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "viewer_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "viewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("story_id", "viewer_id", name="uq_social_story_view"),
    )
    op.create_index("ix_social_story_views_story_id", "social_story_views", ["story_id"])
    op.create_index(
        "ix_social_story_views_viewer_id", "social_story_views", ["viewer_id"]
    )

    op.create_table(
        "social_story_comments",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "story_id",
            UuidCol(),
            sa.ForeignKey("social_stories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_social_story_comments_story_id", "social_story_comments", ["story_id"]
    )
    op.create_index(
        "ix_social_story_comments_author_id", "social_story_comments", ["author_id"]
    )
    op.create_index(
        "ix_social_story_comments_story_created",
        "social_story_comments",
        ["story_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("social_story_comments")
    op.drop_table("social_story_views")
    op.drop_table("social_stories")
