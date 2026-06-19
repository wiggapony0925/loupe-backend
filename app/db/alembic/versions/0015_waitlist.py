"""Create the ``waitlist_entries`` table.

Backs the Loupe Scanner "Join the waitlist" checkout flow and the admin
Waitlist pipeline. Keyed by email (unique) so a repeat signup updates in
place; ``user_id`` links a signed-in visitor to their Loupe account.

Revision ID: 0015_waitlist
Revises: 0014_feature_flags
Create Date: 2026-06-19 20:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0015_waitlist"
down_revision: str | Sequence[str] | None = "0014_feature_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "waitlist_entries",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(160), nullable=True),
        sa.Column("interest", sa.Text(), nullable=True),
        sa.Column("referral_source", sa.String(120), nullable=True),
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="waiting"),
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
        sa.UniqueConstraint("email", name="uq_waitlist_entries_email"),
    )
    op.create_index(
        "ix_waitlist_entries_email", "waitlist_entries", ["email"], unique=True
    )
    op.create_index("ix_waitlist_entries_user_id", "waitlist_entries", ["user_id"])
    op.create_index("ix_waitlist_entries_status", "waitlist_entries", ["status"])
    op.create_index(
        "ix_waitlist_entries_status_created",
        "waitlist_entries",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("waitlist_entries")
