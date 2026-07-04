"""Create email_log — one row per outbound email, queue → delivery.

Written by the send pipeline (queued/sent/failed, with the rendered content
for retries + support previews) and advanced by the Resend webhook
(delivered/bounced/complained). Additive — safe online migration.

Revision ID: 0027_email_log
Revises: 0026_email_verified
Create Date: 2026-07-02 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0027_email_log"
down_revision: str | Sequence[str] | None = "0026_email_verified"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_log",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column("to_email", sa.String(320), nullable=False),
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("category", sa.String(40), nullable=True),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("headers", sa.JSON(), nullable=True),
        sa.Column("from_email", sa.String(320), nullable=True),
        sa.Column("idempotency_key", sa.String(256), nullable=True),
        sa.Column("provider_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_email_log_to_email", "email_log", ["to_email"])
    op.create_index("ix_email_log_user_id", "email_log", ["user_id"])
    op.create_index("ix_email_log_category", "email_log", ["category"])
    op.create_index("ix_email_log_provider_id", "email_log", ["provider_id"])
    op.create_index("ix_email_log_status", "email_log", ["status"])
    op.create_index("ix_email_log_created_at", "email_log", ["created_at"])


def downgrade() -> None:
    op.drop_table("email_log")
