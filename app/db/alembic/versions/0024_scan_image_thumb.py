"""Add card_identifications.image_thumb_b64 — scanned-frame review thumbnail.

Stores a small base64 JPEG (~320px long edge) of the actual photo the user
scanned, so the admin scan-history log can show what was in front of the
camera. Additive + nullable (legacy rows + the text-only fallback path read as
NULL) — safe online migration, no backfill. Postgres TOASTs the value
out-of-line so it never slows the analytics queries that don't select it.

Revision ID: 0024_scan_image_thumb
Revises: 0023_graded_card_acquired_via
Create Date: 2026-07-01 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_scan_image_thumb"
down_revision: str | Sequence[str] | None = "0023_graded_card_acquired_via"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("card_identifications") as batch:
        batch.add_column(sa.Column("image_thumb_b64", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("card_identifications") as batch:
        batch.drop_column("image_thumb_b64")
