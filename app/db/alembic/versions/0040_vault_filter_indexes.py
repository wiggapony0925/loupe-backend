"""Vault filter performance indexes.

Adds composite indexes for common vault filter/sort columns and a reverse
lookup on collection_items so collection-scoped queries stay fast.

Revision ID: 0040_vault_filter_indexes
Revises: 0039_report_collection_scope
Create Date: 2026-07-10 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_vault_filter_indexes"
down_revision: str | Sequence[str] | None = "0039_report_collection_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_graded_cards_user_active",
        "graded_cards",
        ["user_id", "deleted_at"],
        unique=False,
    )
    op.create_index(
        "ix_graded_cards_user_value",
        "graded_cards",
        ["user_id", "estimated_value_usd"],
        unique=False,
    )
    op.create_index(
        "ix_graded_cards_user_grade",
        "graded_cards",
        ["user_id", "grade"],
        unique=False,
    )
    op.create_index(
        "ix_collection_items_graded_card_id",
        "collection_items",
        ["graded_card_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_collection_items_graded_card_id", table_name="collection_items")
    op.drop_index("ix_graded_cards_user_grade", table_name="graded_cards")
    op.drop_index("ix_graded_cards_user_value", table_name="graded_cards")
    op.drop_index("ix_graded_cards_user_active", table_name="graded_cards")
