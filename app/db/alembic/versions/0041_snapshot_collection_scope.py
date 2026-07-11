"""Collection-scoped portfolio snapshots.

`portfolio_snapshots` was keyed only by user — but `summary()`/`history()`
now compute COLLECTION-scoped totals, and capturing those into the same
series poisoned the user's "All" chart with collection-sized values (and
scoped charts spliced All-sized observations back in). Adding a nullable
`collection_id` gives every scope its own honest series:

* NULL  → the whole-vault ("All") series — existing rows keep meaning.
* value → that collection's own intraday/daily observations.

FK is ON DELETE CASCADE so deleting a collection tears down its series
instead of leaving orphaned totals that no chart can explain.

Revision ID: 0041_snapshot_collection_scope
Revises: 0040_vault_filter_indexes
Create Date: 2026-07-11 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_snapshot_collection_scope"
down_revision: str | Sequence[str] | None = "0040_vault_filter_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "portfolio_snapshots",
        sa.Column("collection_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_portfolio_snapshots_collection",
        "portfolio_snapshots",
        "collections",
        ["collection_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_portfolio_snapshots_user_scope_captured",
        "portfolio_snapshots",
        ["user_id", "collection_id", "captured_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_snapshots_user_scope_captured",
        table_name="portfolio_snapshots",
    )
    op.drop_constraint(
        "fk_portfolio_snapshots_collection",
        "portfolio_snapshots",
        type_="foreignkey",
    )
    op.drop_column("portfolio_snapshots", "collection_id")
