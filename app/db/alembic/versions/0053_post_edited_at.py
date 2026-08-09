"""Record when a post's caption was last edited.

Nullable with no backfill: every existing post is unedited, and NULL says
that more honestly than a timestamp copied from ``created_at`` would — the
latter would make the whole table render as "edited" the moment the client
starts showing the marker.

Revision ID: 0053_post_edited_at
Revises: 0052_user_phone
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_post_edited_at"
down_revision = "0052_user_phone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "social_posts",
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("social_posts", "edited_at")
