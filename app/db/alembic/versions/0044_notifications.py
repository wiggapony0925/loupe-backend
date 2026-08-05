"""Notifications — the server-owned inbox.

Replaces a feed the mobile app synthesized on-device (announcement + blog
list + price alerts, merged client-side) with real rows, so read state
survives a reinstall, the list can be paginated, and the backend can tell one
specific user something.

NOTE ON ORDERING: an in-flight `0044_social` branch also chains off 0043. Only
one migration may claim a parent, so whichever lands second must re-point its
``down_revision`` at the other. This one shipped first; social rebases onto
``0044_notifications``.

Revision ID: 0044_notifications
Revises: 0043_ai_search_log_results
Create Date: 2026-08-05 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_notifications"
down_revision: str | Sequence[str] | None = "0043_ai_search_log_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("href", sa.String(500), nullable=True),
        sa.Column("image_url", sa.String(1000), nullable=True),
        sa.Column("data", sa.JSON(), nullable=True),
        # Natural key of the underlying event — the guard against a replayed
        # webhook or a re-run cron posting the same thing twice.
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    # The inbox page: one user, newest first.
    op.create_index(
        "ix_notifications_user_created", "notifications", ["user_id", "created_at"]
    )
    # The unread badge — answerable from the index alone.
    op.create_index(
        "ix_notifications_user_read", "notifications", ["user_id", "read_at"]
    )
    op.create_index(
        "ix_notifications_dedupe_key", "notifications", ["dedupe_key"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_dedupe_key", table_name="notifications")
    op.drop_index("ix_notifications_user_read", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")
