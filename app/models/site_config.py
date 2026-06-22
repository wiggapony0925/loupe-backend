"""SiteConfig — the single, admin-tunable settings row.

A singleton (one row) holding everything the developer portal lets you change
live, without a deploy:

* the Loupe Pro plan shape — the free card/statement limits and which features
  are actually gated behind Pro, and
* a global announcement banner shown to every user.

Read through ``site_config_service.get`` (which lazily creates the row with
sensible defaults), never queried directly, so create-all dev DBs and migrated
prod DBs behave identically.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol


class SiteConfig(Base):
    """Singleton admin configuration (plan shape + announcement)."""

    __tablename__ = "site_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )

    # ── Free-tier limits (null = unlimited) ──
    free_card_limit: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, default=50
    )
    free_statement_limit: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, default=1
    )

    # ── Per-feature gating. True = Pro-only; False = free for everyone. ──
    gate_unlimited_cards: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True
    )
    gate_scanner_import: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True
    )
    gate_full_history: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True
    )
    gate_unlimited_alerts: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True
    )
    gate_statements: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True
    )

    # ── Global announcement banner (shown to all users when enabled) ──
    announcement_enabled: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False
    )
    announcement_message: Mapped[str] = mapped_column(
        String(500), nullable=False, default=""
    )
    # "info" | "success" | "warning" | "error" — drives the banner tone.
    announcement_tone: Mapped[str] = mapped_column(
        String(16), nullable=False, default="info"
    )
    # Bumped whenever the message changes, so clients can re-show a dismissed
    # banner only when it's genuinely new.
    announcement_cta_label: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    announcement_cta_href: Mapped[str | None] = mapped_column(Text(), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


__all__ = ["SiteConfig"]
