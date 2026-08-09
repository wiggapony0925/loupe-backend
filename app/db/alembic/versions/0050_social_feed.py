"""social feed — posts, media, likes, threaded comments, hashtags, mentions

Revision ID: 0050_social_feed
Revises: 0049_saved_stores
Create Date: 2026-08-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision = "0050_social_feed"
down_revision = "0049_saved_stores"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "social_posts",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "author_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=True),
        # SET NULL, not CASCADE: a catalog re-key must not delete posts.
        sa.Column(
            "card_id",
            UuidCol(),
            sa.ForeignKey("cards.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index(
        "ix_social_posts_author_created", "social_posts", ["author_id", "created_at"]
    )
    op.create_index("ix_social_posts_created", "social_posts", ["created_at"])

    op.create_table(
        "social_post_media",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "post_id",
            UuidCol(),
            sa.ForeignKey("social_posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("post_id", "position", name="uq_social_post_media_position"),
    )
    op.create_index("ix_social_post_media_post_id", "social_post_media", ["post_id"])

    op.create_table(
        "social_post_likes",
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "post_id",
            UuidCol(),
            sa.ForeignKey("social_posts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_social_post_likes_post", "social_post_likes", ["post_id"])

    op.create_table(
        "social_post_comments",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "post_id",
            UuidCol(),
            sa.ForeignKey("social_posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_id",
            UuidCol(),
            sa.ForeignKey("social_post_comments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_social_post_comments_post_created",
        "social_post_comments",
        ["post_id", "created_at"],
    )
    op.create_index(
        "ix_social_post_comments_parent", "social_post_comments", ["parent_id"]
    )
    op.create_index(
        "ix_social_post_comments_author_id", "social_post_comments", ["author_id"]
    )

    op.create_table(
        "social_comment_likes",
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "comment_id",
            UuidCol(),
            sa.ForeignKey("social_post_comments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_social_comment_likes_comment", "social_comment_likes", ["comment_id"]
    )

    op.create_table(
        "social_post_hashtags",
        sa.Column(
            "post_id",
            UuidCol(),
            sa.ForeignKey("social_posts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tag", sa.String(length=64), primary_key=True),
    )
    op.create_index("ix_social_post_hashtags_tag", "social_post_hashtags", ["tag"])

    op.create_table(
        "social_post_mentions",
        sa.Column(
            "post_id",
            UuidCol(),
            sa.ForeignKey("social_posts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index(
        "ix_social_post_mentions_user", "social_post_mentions", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_social_post_mentions_user", table_name="social_post_mentions")
    op.drop_table("social_post_mentions")
    op.drop_index("ix_social_post_hashtags_tag", table_name="social_post_hashtags")
    op.drop_table("social_post_hashtags")
    op.drop_index("ix_social_comment_likes_comment", table_name="social_comment_likes")
    op.drop_table("social_comment_likes")
    op.drop_index(
        "ix_social_post_comments_author_id", table_name="social_post_comments"
    )
    op.drop_index("ix_social_post_comments_parent", table_name="social_post_comments")
    op.drop_index(
        "ix_social_post_comments_post_created", table_name="social_post_comments"
    )
    op.drop_table("social_post_comments")
    op.drop_index("ix_social_post_likes_post", table_name="social_post_likes")
    op.drop_table("social_post_likes")
    op.drop_index("ix_social_post_media_post_id", table_name="social_post_media")
    op.drop_table("social_post_media")
    op.drop_index("ix_social_posts_created", table_name="social_posts")
    op.drop_index("ix_social_posts_author_created", table_name="social_posts")
    op.drop_table("social_posts")
