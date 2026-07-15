"""Loupe AI ask log — the chatbot's flight recorder.

One row per /cards/search/ai ask: who asked what, the model's answer
(message + candidate names), how it was served (ai vs fallback, cache hit),
latency, and the user's thumbs up/down verdict. Powers the /admin/ai
conversations dev tool and accuracy analytics; nothing user-facing reads it.

Revision ID: 0042_ai_search_log
Revises: 0041_snapshot_collection_scope
Create Date: 2026-07-15 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_ai_search_log"
down_revision: str | Sequence[str] | None = "0041_snapshot_collection_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_search_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("query", sa.String(220), nullable=False),
        sa.Column("game_hint", sa.String(20), nullable=True),
        sa.Column("game", sa.String(20), nullable=True),
        sa.Column("source", sa.String(12), nullable=False),
        sa.Column(
            "cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("candidates", sa.JSON(), nullable=True),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("feedback", sa.SmallInteger(), nullable=True),
        sa.Column("feedback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_ai_search_log_user_id", "ai_search_log", ["user_id"])
    op.create_index("ix_ai_search_log_game", "ai_search_log", ["game"])
    op.create_index("ix_ai_search_log_source", "ai_search_log", ["source"])
    op.create_index("ix_ai_search_log_feedback", "ai_search_log", ["feedback"])
    op.create_index("ix_ai_search_log_created_at", "ai_search_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_search_log_created_at", table_name="ai_search_log")
    op.drop_index("ix_ai_search_log_feedback", table_name="ai_search_log")
    op.drop_index("ix_ai_search_log_source", table_name="ai_search_log")
    op.drop_index("ix_ai_search_log_game", table_name="ai_search_log")
    op.drop_index("ix_ai_search_log_user_id", table_name="ai_search_log")
    op.drop_table("ai_search_log")
