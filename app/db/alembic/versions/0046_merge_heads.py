"""Merge the 0044 fork back to a single head.

``0044_notifications`` and ``0044_social`` were both cut from 0043 in
parallel sessions and BOTH branches are already applied in production
(the migrate job runs ``upgrade heads``, so ``alembic_version`` carries two
rows). Rebasing one branch under the other at this point would rewrite
lineage prod has already recorded — a no-op merge revision is the only
history-safe way back to one head, and it's what ``test_alembic_single_head``
enforces from now on.

Revision ID: 0046_merge_heads
Revises: 0044_notifications, 0045_social_engagement
Create Date: 2026-08-05 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0046_merge_heads"
down_revision: str | Sequence[str] | None = (
    "0044_notifications",
    "0045_social_engagement",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
