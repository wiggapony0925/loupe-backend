"""Add login-lockout + TOTP MFA columns to users.

Hardens admin (and all) accounts: brute-force lockout state
(``failed_login_count`` / ``locked_until``) and two-factor auth
(``mfa_secret`` sealed at rest, ``mfa_enabled``, hashed ``mfa_backup_codes``,
``mfa_enrolled_at``). All additive + nullable/defaulted — safe online migration,
no backfill.

Revision ID: 0021_auth_hardening
Revises: 0020_user_recents
Create Date: 2026-06-22 23:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_auth_hardening"
down_revision: str | Sequence[str] | None = "0020_user_recents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        # ── Brute-force lockout ──
        batch.add_column(
            sa.Column(
                "failed_login_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True)
        )
        # ── TOTP MFA ──
        # Sealed TOTP secret ("f:<fernet>" when MFA_SECRET_KEY is set, else
        # "p:<base32>"). Held only while enrolling + while enabled.
        batch.add_column(sa.Column("mfa_secret", sa.String(length=255), nullable=True))
        batch.add_column(
            sa.Column(
                "mfa_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        # JSON list of argon2 hashes of one-time recovery codes.
        batch.add_column(sa.Column("mfa_backup_codes", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("mfa_enrolled_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("mfa_enrolled_at")
        batch.drop_column("mfa_backup_codes")
        batch.drop_column("mfa_enabled")
        batch.drop_column("mfa_secret")
        batch.drop_column("locked_until")
        batch.drop_column("failed_login_count")
