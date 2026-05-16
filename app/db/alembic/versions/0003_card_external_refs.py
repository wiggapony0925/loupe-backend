"""Add ``card_external_refs`` table.

Revision ID: 0003_card_external_refs
Revises: 0002_password_hash
Create Date: 2026-05-16 01:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0003_card_external_refs"
down_revision: str | Sequence[str] | None = "0002_password_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "card_external_refs",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "card_id",
            UuidCol(),
            sa.ForeignKey("cards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "source", "external_id", name="uq_card_external_refs_src_id"
        ),
    )
    op.create_index(
        "ix_card_external_refs_card_id", "card_external_refs", ["card_id"]
    )
    op.create_index(
        "ix_card_external_refs_source", "card_external_refs", ["source"]
    )
    op.create_index(
        "ix_card_external_refs_card_source",
        "card_external_refs",
        ["card_id", "source"],
    )


def downgrade() -> None:
    op.drop_index("ix_card_external_refs_card_source", table_name="card_external_refs")
    op.drop_index("ix_card_external_refs_source", table_name="card_external_refs")
    op.drop_index("ix_card_external_refs_card_id", table_name="card_external_refs")
    op.drop_table("card_external_refs")
