"""Create the ``site_config`` singleton (plan shape + announcement banner).

One admin-tunable row: free-tier limits, per-feature Pro gating, and a global
announcement. Seeds a default row matching today's behaviour (50-card cap, all
features gated, announcement off).

Revision ID: 0019_site_config
Revises: 0018_user_plan
Create Date: 2026-06-22 13:30:00
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0019_site_config"
down_revision: str | Sequence[str] | None = "0018_user_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_config",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column("free_card_limit", sa.Integer(), nullable=True),
        sa.Column("free_statement_limit", sa.Integer(), nullable=True),
        sa.Column(
            "gate_unlimited_cards",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "gate_scanner_import",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "gate_full_history", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "gate_unlimited_alerts",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "gate_statements", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "announcement_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "announcement_message", sa.String(500), nullable=False, server_default=""
        ),
        sa.Column(
            "announcement_tone", sa.String(16), nullable=False, server_default="info"
        ),
        sa.Column("announcement_cta_label", sa.String(80), nullable=True),
        sa.Column("announcement_cta_href", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    cfg = sa.table(
        "site_config",
        sa.column("id", UuidCol()),
        sa.column("free_card_limit", sa.Integer),
        sa.column("free_statement_limit", sa.Integer),
    )
    op.bulk_insert(
        cfg,
        [{"id": uuid.uuid4(), "free_card_limit": 50, "free_statement_limit": 1}],
    )


def downgrade() -> None:
    op.drop_table("site_config")
