"""Add user_settings.email_announcements_enabled — announcement email opt-out.

Backs the one-click unsubscribe link in blog/product-update emails (CAN-SPAM).
Transactional mail is not gated on it. Additive with a server default of TRUE
(every existing user keeps receiving announcements until they opt out) — safe
online migration, no backfill.

Revision ID: 0025_email_announcements_pref
Revises: 0024_scan_image_thumb
Create Date: 2026-07-02 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_email_announcements_pref"
down_revision: str | Sequence[str] | None = "0024_scan_image_thumb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings") as batch:
        batch.add_column(
            sa.Column(
                "email_announcements_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("user_settings") as batch:
        batch.drop_column("email_announcements_enabled")
