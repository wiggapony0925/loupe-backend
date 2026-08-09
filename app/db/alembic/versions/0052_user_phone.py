"""user phone — E.164, unique, verification-ready

Revision ID: 0052_user_phone
Revises: 0051_social_moderation
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_user_phone"
down_revision = "0051_social_moderation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(length=20), nullable=True))
    op.add_column(
        "users",
        sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # UNIQUE for the same reason email is: a phone number is an identity, and
    # a future SMS login has to resolve it to exactly one account. Created as
    # a unique INDEX (not a constraint) so it also serves the lookup.
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_phone", table_name="users")
    op.drop_column("users", "phone_verified_at")
    op.drop_column("users", "phone")
