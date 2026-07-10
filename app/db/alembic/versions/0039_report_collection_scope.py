"""Collection-scoped statements — add collection_id/_name to user_reports.

Lets a user generate a PDF statement for one collection (portfolio) in
addition to the whole vault. `collection_id` is NOT an FK so deleting a
collection never cascades away historical PDFs; `collection_name` is baked
at generation time so archive rows stay labelled forever.

The unique constraint widens to include the scope so "May 2026 · Binder A"
and "May 2026" (whole vault) can coexist.

Revision ID: 0039_report_collection_scope
Revises: 0038_active_collection_pref
Create Date: 2026-07-09 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_report_collection_scope"
down_revision: str | Sequence[str] | None = "0038_active_collection_pref"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_reports") as batch:
        batch.add_column(sa.Column("collection_id", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column("collection_name", sa.String(length=120), nullable=True)
        )
        batch.drop_constraint(
            "uq_user_reports_user_period_start", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_user_reports_user_period_start_scope",
            ["user_id", "period", "period_start", "collection_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("user_reports") as batch:
        batch.drop_constraint(
            "uq_user_reports_user_period_start_scope", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_user_reports_user_period_start",
            ["user_id", "period", "period_start"],
        )
        batch.drop_column("collection_name")
        batch.drop_column("collection_id")
