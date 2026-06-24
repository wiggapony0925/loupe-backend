"""Add users.token_version for stateless session revocation.

Every access/refresh token carries the ``token_version`` current at sign-in as a
``ver`` claim; bumping the column invalidates all of a user's outstanding tokens
(the "sign out everywhere" / revoke-a-stolen-token kill switch). Additive,
non-null with a ``0`` server default — safe online migration, no backfill.

Revision ID: 0022_token_version
Revises: 0021_auth_hardening
Create Date: 2026-06-24 17:55:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_token_version"
down_revision: str | Sequence[str] | None = "0021_auth_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "token_version",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("token_version")
