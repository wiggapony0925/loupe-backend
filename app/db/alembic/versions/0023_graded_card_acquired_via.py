"""Add graded_cards.acquired_via — how a card entered the collection.

scan | manual | import. Additive + nullable (legacy rows read as "unknown") —
safe online migration, no backfill. `is_graded` stays a computed property on the
model (derived from `house`), so no column is needed for it.

Revision ID: 0023_graded_card_acquired_via
Revises: 0022_token_version
Create Date: 2026-06-25 03:05:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_graded_card_acquired_via"
down_revision: str | Sequence[str] | None = "0022_token_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUM = sa.Enum("scan", "manual", "import", name="acquisition_source_enum")


def upgrade() -> None:
    bind = op.get_bind()
    _ENUM.create(bind, checkfirst=True)
    with op.batch_alter_table("graded_cards") as batch:
        batch.add_column(sa.Column("acquired_via", _ENUM, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("graded_cards") as batch:
        batch.drop_column("acquired_via")
    _ENUM.drop(op.get_bind(), checkfirst=True)
