"""Add ``language`` to catalog_mirror_cards (per-printing ISO language).

Backs the client language picker + the search ``langs`` filter: a user can mix
languages or stay on English (the default). Existing rows backfill to "en" via
the server default — no data migration, safe online (add-column + index).

Revision ID: 0034_mirror_language
Revises: 0033_portfolio_snapshots
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_mirror_language"
down_revision: str | Sequence[str] | None = "0033_portfolio_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "catalog_mirror_cards",
        sa.Column(
            "language",
            sa.String(length=8),
            nullable=False,
            server_default="en",
        ),
    )
    op.create_index(
        "ix_catalog_mirror_cards_language",
        "catalog_mirror_cards",
        ["tcg", "language"],
    )


def downgrade() -> None:
    op.drop_index("ix_catalog_mirror_cards_language", table_name="catalog_mirror_cards")
    op.drop_column("catalog_mirror_cards", "language")
