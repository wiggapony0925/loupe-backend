"""Add users.email_verified_at — signed-link email verification.

NULL means unverified; nothing is gated on it (trust signal + nudge for now).
Backfills Apple/Google accounts as verified — those providers verify the
address before we ever see it. Additive + nullable — safe online migration.

Revision ID: 0026_email_verified
Revises: 0025_email_announcements_pref
Create Date: 2026-07-02 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_email_verified"
down_revision: str | Sequence[str] | None = "0025_email_announcements_pref"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.execute(
        "UPDATE users SET email_verified_at = CURRENT_TIMESTAMP "
        "WHERE apple_subject IS NOT NULL OR google_subject IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("email_verified_at")
