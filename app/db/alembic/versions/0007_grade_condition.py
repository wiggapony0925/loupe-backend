"""Add raw-condition column to ``graded_cards``.

`condition` captures PSA-style raw-card vocabulary (NM/LP/MP/HP/DMG) for
ungraded holdings. Only meaningful when ``house == loupe`` (our slug for
"raw / not slabbed") — slabbed cards already encode condition in the
grade number. Nullable so existing rows + slabbed grades stay valid; the
UI hides the chip when the card has a third-party grade.

Revision ID: 0007_grade_condition
Revises: 0006_user_reports
Create Date: 2026-05-25 20:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_grade_condition"
down_revision: str | Sequence[str] | None = "0006_user_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONDITION_VALUES = ("nm", "lp", "mp", "hp", "dmg")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # CREATE TYPE on first run; subsequent revisions are no-ops.
        sa.Enum(
            *_CONDITION_VALUES, name="raw_condition_enum", create_type=True
        ).create(bind, checkfirst=True)
        op.add_column(
            "graded_cards",
            sa.Column(
                "condition",
                sa.Enum(
                    *_CONDITION_VALUES,
                    name="raw_condition_enum",
                    create_type=False,
                ),
                nullable=True,
            ),
        )
    else:
        # SQLite (test) and other dialects: store as VARCHAR with a CHECK.
        op.add_column(
            "graded_cards",
            sa.Column(
                "condition",
                sa.Enum(*_CONDITION_VALUES, name="raw_condition_enum"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column("graded_cards", "condition")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(*_CONDITION_VALUES, name="raw_condition_enum").drop(
            bind, checkfirst=True
        )
