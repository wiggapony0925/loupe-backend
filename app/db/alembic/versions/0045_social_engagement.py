"""Profile engagement: likes and unique-viewer counts.

Two edge tables beside the follow graph from 0044:

* ``social_profile_likes``  — one collector appreciating another's collection.
  An edge rather than a counter column so "unlike" is implementable and the
  count always has a verifiable source.
* ``social_profile_visits`` — one row per DISTINCT viewer of a profile,
  upserted on repeat visits. Counting raw hits instead would let a single
  refresh inflate the figure and would grow this table without bound.

Both are keyed (actor, profile) so the uniqueness is enforced by the primary
key — there is no path to a double-count.

Revision ID: 0045_social_engagement
Revises: 0044_social
Create Date: 2026-08-04 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0045_social_engagement"
down_revision: str | Sequence[str] | None = "0044_social"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "social_profile_likes",
        sa.Column(
            "liker_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "profile_user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "liker_id != profile_user_id", name="ck_social_like_not_self"
        ),
    )
    # The PK already covers (liker → profile). Counting likes for a profile
    # and answering "have I liked this" both read the other direction.
    op.create_index(
        "ix_social_profile_likes_profile", "social_profile_likes", ["profile_user_id"]
    )

    op.create_table(
        "social_profile_visits",
        sa.Column(
            "viewer_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "profile_user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "viewer_id != profile_user_id", name="ck_social_visit_not_self"
        ),
    )
    op.create_index(
        "ix_social_profile_visits_profile", "social_profile_visits", ["profile_user_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_social_profile_visits_profile", table_name="social_profile_visits"
    )
    op.drop_table("social_profile_visits")
    op.drop_index("ix_social_profile_likes_profile", table_name="social_profile_likes")
    op.drop_table("social_profile_likes")
