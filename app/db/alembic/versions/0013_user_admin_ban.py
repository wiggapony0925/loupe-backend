"""Add admin + ban columns to users.

Backs the developer portal's user management: a DB-backed admin grant
(complementing the ``ADMIN_EMAILS`` bootstrap allowlist) and a ban state
that auth rejects (like soft-delete) while retaining the row + reason.

Revision ID: 0013_user_admin_ban
Revises: 0012_portal
Create Date: 2026-06-19 18:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_user_admin_ban"
down_revision: str | Sequence[str] | None = "0012_portal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "is_admin",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("ban_reason", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("ban_reason")
        batch.drop_column("banned_at")
        batch.drop_column("is_admin")
