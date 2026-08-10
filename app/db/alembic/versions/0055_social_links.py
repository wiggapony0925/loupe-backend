"""Per-platform social links on collector profiles.

One nullable JSON column (`{platform: canonical https URL}`) rather than a
link table: the platform set is a service-enforced allowlist, the dict is
read and written whole alongside the profile, and no query ever asks "who
links to X" — so rows would add a join and buy nothing. Nullable with no
backfill: every existing profile simply has no links yet.

Revision ID: 0055_social_links
Revises: 0054_social_stories
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_social_links"
down_revision = "0054_social_stories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "social_profiles",
        sa.Column("links", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("social_profiles", "links")
