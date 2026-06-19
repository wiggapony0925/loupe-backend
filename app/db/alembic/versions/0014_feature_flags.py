"""Create the ``feature_flags`` table + seed a few starter flags.

Runtime on/off switches for pages, components, and micro-apps across web
and mobile. Seeds the flags the clients already gate on (enabled by default,
so nothing is hidden until an admin flips one off).

Revision ID: 0014_feature_flags
Revises: 0013_user_admin_ban
Create Date: 2026-06-19 19:00:00
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0014_feature_flags"
down_revision: str | Sequence[str] | None = "0013_user_admin_ban"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED = [
    (
        "web_markets",
        "Markets (web)",
        "The Markets micro-app in the web dashboard.",
        True,
    ),
    (
        "web_watchlist",
        "Watchlist (web)",
        "The Watchlist page in the web dashboard.",
        True,
    ),
    ("web_analytics", "Analytics (web)", "The portfolio Analytics page on web.", True),
    (
        "mobile_scan",
        "Card scanning (mobile)",
        "On-device card scanning in the mobile app.",
        True,
    ),
    (
        "promo_banner",
        "Promo banner",
        "A site-wide marketing banner. Off by default.",
        False,
    ),
]


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
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
        sa.UniqueConstraint("key", name="uq_feature_flags_key"),
    )
    op.create_index("ix_feature_flags_key", "feature_flags", ["key"], unique=True)

    flags = sa.table(
        "feature_flags",
        sa.column("id", UuidCol()),
        sa.column("key", sa.String),
        sa.column("label", sa.String),
        sa.column("description", sa.Text),
        sa.column("enabled", sa.Boolean),
    )
    op.bulk_insert(
        flags,
        [
            {
                "id": uuid.uuid4(),
                "key": k,
                "label": label,
                "description": desc,
                "enabled": enabled,
            }
            for (k, label, desc, enabled) in _SEED
        ],
    )


def downgrade() -> None:
    op.drop_table("feature_flags")
