"""Store WHICH cards each AI answer showed.

`ai_search_log.results` keeps a compact snapshot (id, name, set, rarity,
image, price) of the cards the user actually saw under the bubble, so the
/admin/ai drill-in can replay the full exchange — question, answer, AND the
shelf — when judging a thumbs-down.

Revision ID: 0043_ai_search_log_results
Revises: 0042_ai_search_log
Create Date: 2026-07-15 00:00:01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_ai_search_log_results"
down_revision: str | Sequence[str] | None = "0042_ai_search_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_search_log", sa.Column("results", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_search_log", "results")
