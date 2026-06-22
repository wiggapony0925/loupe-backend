"""Add subscription/plan columns to users + seed the subscriptions kill switch.

Backs Loupe Pro: a per-user ``plan`` (free|pro) with optional expiry, plus a
``stripe_customer_id`` placeholder so wiring Stripe later is a no-migration
change. Also seeds the ``subscriptions_enabled`` feature flag **off**, so the
app behaves exactly as it does today (everything free, no paywall) until an
admin flips the flag on from the developer portal.

Revision ID: 0018_user_plan
Revises: 0017_sealed_price_history
Create Date: 2026-06-22 12:00:00
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0018_user_plan"
down_revision: str | Sequence[str] | None = "0017_sealed_price_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "plan",
                sa.String(length=16),
                nullable=False,
                server_default="free",
            )
        )
        batch.add_column(
            sa.Column("pro_since", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "pro_trialing",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("pro_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("stripe_customer_id", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("stripe_subscription_id", sa.String(length=64), nullable=True)
        )

    # The global kill switch. Off => the entitlement layer treats every user
    # as Pro (no limits, no paywall), so a billing failure with real users can
    # never break the app. Flip on from the portal to turn gating on.
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
                "key": "subscriptions_enabled",
                "label": "Subscriptions (Loupe Pro)",
                "description": (
                    "Master switch for Loupe Pro. Off = everyone is treated as "
                    "Pro (no card limit, no paywall) — the safe default for "
                    "testing. On = free-tier limits and the upgrade paywall "
                    "are active."
                ),
                "enabled": False,
            }
        ],
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_flags WHERE key = 'subscriptions_enabled'"))
    with op.batch_alter_table("users") as batch:
        batch.drop_column("stripe_subscription_id")
        batch.drop_column("stripe_customer_id")
        batch.drop_column("pro_expires_at")
        batch.drop_column("pro_trialing")
        batch.drop_column("pro_since")
        batch.drop_column("plan")
