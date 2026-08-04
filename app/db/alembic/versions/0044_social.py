"""The social layer: profiles, the follow graph, and follow requests.

Three tables under ``app/social``:

* ``social_profiles``       — claimable @username, bio, self-reported
  location, private-account flag, and profile-picture pointers (1:1 users).
* ``social_follows``        — accepted follower edges.
* ``social_follow_requests``— pending asks to follow a private account.

Also seeds the ``web_social`` feature flag (enabled) gating the Community
surface on web — flip it off in /admin to hide the whole feature without
a deploy.

Revision ID: 0044_social
Revises: 0043_ai_search_log_results
Create Date: 2026-08-04 00:00:00
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from app.db.types import UuidCol

revision: str = "0044_social"
down_revision: str | Sequence[str] | None = "0043_ai_search_log_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "social_profiles",
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("username", sa.String(30), nullable=False),
        sa.Column("bio", sa.String(280), nullable=True),
        sa.Column("location", sa.String(120), nullable=True),
        sa.Column(
            "is_private", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("avatar_key", sa.String(255), nullable=True),
        sa.Column("avatar_content_type", sa.String(64), nullable=True),
        sa.Column("avatar_version", sa.Integer(), nullable=False, server_default="0"),
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
        "ix_social_profiles_username", "social_profiles", ["username"], unique=True
    )

    op.create_table(
        "social_follows",
        sa.Column(
            "follower_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "followee_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "follower_id != followee_id", name="ck_social_follow_not_self"
        ),
    )
    op.create_index("ix_social_follows_followee", "social_follows", ["followee_id"])

    op.create_table(
        "social_follow_requests",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "requester_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "requester_id", "target_id", name="uq_social_follow_request"
        ),
        sa.CheckConstraint(
            "requester_id != target_id", name="ck_social_request_not_self"
        ),
    )
    op.create_index(
        "ix_social_follow_requests_requester_id",
        "social_follow_requests",
        ["requester_id"],
    )
    op.create_index(
        "ix_social_follow_requests_target_id",
        "social_follow_requests",
        ["target_id"],
    )

    # Feature flag for the web Community surface (idempotent insert).
    flags = sa.table(
        "feature_flags",
        sa.column("id", UuidCol()),
        sa.column("key", sa.String),
        sa.column("label", sa.String),
        sa.column("description", sa.Text),
        sa.column("enabled", sa.Boolean),
    )
    # The SELECT pre-check needs a live connection; in offline (--sql) mode
    # just emit the INSERT — a fresh render is for a DB that lacks the flag.
    existing = None
    if not context.is_offline_mode():
        existing = (
            op.get_bind()
            .execute(sa.text("SELECT 1 FROM feature_flags WHERE key = 'web_social'"))
            .first()
        )
    if not existing:
        op.bulk_insert(
            flags,
            [
                {
                    "id": uuid.uuid4(),
                    "key": "web_social",
                    "label": "Community (web)",
                    "description": (
                        "The social layer: collector profiles, follows, and "
                        "shared collections."
                    ),
                    "enabled": True,
                }
            ],
        )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_flags WHERE key = 'web_social'"))
    op.drop_index(
        "ix_social_follow_requests_target_id", table_name="social_follow_requests"
    )
    op.drop_index(
        "ix_social_follow_requests_requester_id",
        table_name="social_follow_requests",
    )
    op.drop_table("social_follow_requests")
    op.drop_index("ix_social_follows_followee", table_name="social_follows")
    op.drop_table("social_follows")
    op.drop_index("ix_social_profiles_username", table_name="social_profiles")
    op.drop_table("social_profiles")
