"""Initial schema for loupe-backend.

Revision ID: 0001_initial
Revises:
Create Date: 2024-05-15 00:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.db import Base
from app import models  # noqa: F401  -- ensures every model is registered

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all tables defined on :data:`app.db.Base.metadata`."""
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    """Drop all tables (reverse of :func:`upgrade`)."""
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
