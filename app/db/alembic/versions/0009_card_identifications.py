"""Create ``card_identifications`` + ``identification_feedback`` tables.

Persists every ``POST /v1/cards/identify`` invocation so we can compute
accuracy / cost analytics over time, and stores user thumbs-up/down
feedback that feeds back into the re-ranker as a popularity prior.

Revision ID: 0009_card_identifications
Revises: 0008_sealed_products
Create Date: 2026-05-26 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.types import JsonCol, UuidCol

revision: str = "0009_card_identifications"
down_revision: str | Sequence[str] | None = "0008_sealed_products"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "card_identifications",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("image_sha256", sa.String(64), nullable=False),
        sa.Column("phash", sa.String(128), nullable=True),
        sa.Column("ocr_provider", sa.String(40), nullable=False),
        sa.Column(
            "ocr_full_text", sa.String(8000), nullable=False, server_default=""
        ),
        sa.Column(
            "ocr_confidence", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column("parsed_title", sa.String(200), nullable=True),
        sa.Column("parsed_set_code", sa.String(20), nullable=True),
        sa.Column("parsed_card_number", sa.String(20), nullable=True),
        sa.Column("tcg_inferred", sa.String(16), nullable=False),
        sa.Column("primary_source", sa.String(16), nullable=False),
        sa.Column("top_card_id", sa.String(64), nullable=True),
        sa.Column("top_upstream_id", sa.String(120), nullable=True),
        sa.Column(
            "top_confidence", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column("candidates_json", JsonCol, nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_card_identifications_user_id", "card_identifications", ["user_id"]
    )
    op.create_index(
        "ix_card_identifications_image_sha256",
        "card_identifications",
        ["image_sha256"],
    )
    op.create_index(
        "ix_card_identifications_created_at",
        "card_identifications",
        ["created_at"],
    )
    op.create_index(
        "ix_card_identifications_tcg_provider",
        "card_identifications",
        ["tcg_inferred", "ocr_provider"],
    )
    # Functional index on lower(parsed_title) — Postgres only; SQLite's
    # automatic indexes already cover the equality predicate well enough
    # for the test suite.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_card_identifications_parsed_title_lower "
            "ON card_identifications (LOWER(parsed_title))"
        )

    op.create_table(
        "identification_feedback",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "identification_id",
            UuidCol(),
            sa.ForeignKey("card_identifications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.Column("chosen_card_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_identification_feedback_identification_id",
        "identification_feedback",
        ["identification_id"],
    )
    op.create_index(
        "ix_identification_feedback_user_id",
        "identification_feedback",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_identification_feedback_user_id", table_name="identification_feedback"
    )
    op.drop_index(
        "ix_identification_feedback_identification_id",
        table_name="identification_feedback",
    )
    op.drop_table("identification_feedback")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_card_identifications_parsed_title_lower")
    op.drop_index(
        "ix_card_identifications_tcg_provider", table_name="card_identifications"
    )
    op.drop_index(
        "ix_card_identifications_created_at", table_name="card_identifications"
    )
    op.drop_index(
        "ix_card_identifications_image_sha256", table_name="card_identifications"
    )
    op.drop_index(
        "ix_card_identifications_user_id", table_name="card_identifications"
    )
    op.drop_table("card_identifications")
