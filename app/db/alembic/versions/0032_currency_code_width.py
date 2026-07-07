"""Widen user_settings.currency from 3 to 6 chars.

The display-currency catalog shared by the mobile + web clients includes
crypto tickers (USDC, MATIC…) that are 4–5 characters, which a `String(3)`
column rejects. Widening is metadata-only on Postgres (no table rewrite) —
safe online migration, no backfill.

Revision ID: 0032_currency_code_width
Revises: 0031_catalog_mirror
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_currency_code_width"
down_revision: str | Sequence[str] | None = "0031_catalog_mirror"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings") as batch:
        batch.alter_column(
            "currency",
            existing_type=sa.String(length=3),
            type_=sa.String(length=6),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Truncate any long codes back to 3 chars before narrowing so the
    # ALTER can't fail on existing rows.
    op.execute("UPDATE user_settings SET currency = SUBSTR(currency, 1, 3)")
    with op.batch_alter_table("user_settings") as batch:
        batch.alter_column(
            "currency",
            existing_type=sa.String(length=6),
            type_=sa.String(length=3),
            existing_nullable=False,
        )
