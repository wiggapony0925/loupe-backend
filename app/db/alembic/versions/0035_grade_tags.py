"""Add ``tags`` to graded_cards (user organization tags).

Per-holding JSON array of short strings ("PC", "For sale", "Trade", …) that let
a collector organize + filter their vault. Nullable — legacy rows stay valid and
readers coalesce null → []. Safe online (add a nullable column, no backfill).

Revision ID: 0035_grade_tags
Revises: 0034_mirror_language
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_grade_tags"
down_revision: str | Sequence[str] | None = "0034_mirror_language"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("graded_cards", sa.Column("tags", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("graded_cards", "tags")
