"""Point-in-time portfolio value snapshots.

Card prices tick at most daily, so the 1D portfolio chart used to be two
buckets (yesterday's close → live now) — a flat line whenever the market
hadn't moved. Snapshots make intraday REAL: every time a signed-in client
computes the live canonical total (vault summary / portfolio history),
the backend opportunistically persists it — at most one row per user per
throttle window — and the 1D series splices those true observations in
between the anchors.

Write-on-read by design: no worker or scheduler dependency (prod's Redis
worker is offline), bounded growth (≤ ~48 rows/user/day, 7-day retention
enforced on the write path).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Scope of the captured total. NULL = the whole-vault ("All") series;
    #: a value = that collection's own series. Every scope charts only its
    #: own observations — mixing them poisoned the 1D chart when collection
    #: scoping shipped (an Umbreon-sized total landing in the All series).
    collection_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(),
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=True,
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    #: Canonical grade-aware total (`holding_value_usd` basis) — the same
    #: number the vault summary / analytics overview report.
    total_value_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    holdings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_portfolio_snapshots_user_captured", "user_id", "captured_at"),
        Index(
            "ix_portfolio_snapshots_user_scope_captured",
            "user_id",
            "collection_id",
            "captured_at",
        ),
        # The scope FK needs an index LEADING with it, which neither of the
        # above provides (collection_id is the second column of one and absent
        # from the other). It is ON DELETE CASCADE, so deleting one binder
        # otherwise scans the whole snapshots table to find the rows to cascade
        # into — and this table grows ~48 rows/user/day regardless of how many
        # collections exist, so "delete a binder" gets slower the longer the
        # product has been running.
        Index("ix_portfolio_snapshots_collection_id", "collection_id"),
    )


__all__ = ["PortfolioSnapshot"]
