"""Add user_settings.active_collection_id — the shared active-portfolio scope.

Stores which collection ("portfolio") the user is viewing so the choice follows
them across mobile and web, exactly like `currency`. NULL = the "All" view
(everything owned). Nullable, no server default, not an FK — a stale id just
falls back to All, so a deleted collection needs no cascade. Additive + safe
online, no backfill.

Revision ID: 0038_active_collection_pref
Revises: 0037_card_embeddings
Create Date: 2026-07-09 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_active_collection_pref"
down_revision: str | Sequence[str] | None = "0037_card_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings") as batch:
        batch.add_column(
            sa.Column("active_collection_id", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("user_settings") as batch:
        batch.drop_column("active_collection_id")
