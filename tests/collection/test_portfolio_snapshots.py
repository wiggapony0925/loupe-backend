"""Portfolio snapshots — real intraday points for the 1D chart.

Proves the three behaviors that make the 1D range honest:
  1. Live-total reads capture a snapshot, at most once per throttle window.
  2. Old rows are purged on the write path (bounded table).
  3. The 1D history splices captured intraday observations between the
     yesterday-close and live-now anchors — no more two-point flat line.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.models.user import User
from app.services.collection import portfolio_service
from tests.factories import make_card


async def _mk_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"snap-{uuid.uuid4().hex[:8]}@test.dev",
        display_name="Snap",
    )
    db.add(user)
    await db.commit()
    return user


async def _mk_holding(db, user: User, value: str) -> GradedCard:
    card = await make_card(db, name="Snapshot Card")
    g = GradedCard(
        user_id=user.id,
        card_id=card.id,
        grade=Decimal("9"),
        house=GradeHouseEnum.psa,
        estimated_value_usd=Decimal(value),
    )
    db.add(g)
    await db.commit()
    return g


@pytest.mark.anyio
async def test_capture_is_throttled(db_session):
    user = await _mk_user(db_session)
    first = await portfolio_service.maybe_capture_snapshot(db_session, user, 100.0, 1)
    second = await portfolio_service.maybe_capture_snapshot(db_session, user, 105.0, 1)

    assert first is True
    assert second is False, "second capture within the window must be skipped"
    rows = (
        (
            await db_session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert float(rows[0].total_value_usd) == 100.0


@pytest.mark.anyio
async def test_capture_skips_empty_vaults(db_session):
    user = await _mk_user(db_session)
    captured = await portfolio_service.maybe_capture_snapshot(db_session, user, 0.0, 0)
    assert captured is False


@pytest.mark.anyio
async def test_retention_purges_rows_past_the_window(db_session):
    """Rows beyond the ~400-day retention purge; recent daily rows survive
    (they ARE the long-range chart now)."""
    user = await _mk_user(db_session)
    ancient = PortfolioSnapshot(
        user_id=user.id,
        captured_at=datetime.now(UTC) - timedelta(days=500),
        total_value_usd=Decimal("10.00"),
        holdings_count=1,
    )
    monthly = PortfolioSnapshot(
        user_id=user.id,
        captured_at=datetime.now(UTC) - timedelta(days=30),
        total_value_usd=Decimal("50.00"),
        holdings_count=1,
    )
    db_session.add_all([ancient, monthly])
    await db_session.commit()

    captured = await portfolio_service.maybe_capture_snapshot(
        db_session, user, 120.0, 2
    )
    assert captured is True

    remaining = (
        (
            await db_session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    values = sorted(float(r.total_value_usd) for r in remaining)
    assert values == [50.0, 120.0], "500d row purged; 30d daily row kept"


@pytest.mark.anyio
async def test_1d_history_splices_intraday_snapshots(db_session):
    user = await _mk_user(db_session)
    await _mk_holding(db_session, user, "200.00")

    # Two real observations captured earlier today.
    now = datetime.now(UTC)
    for hours_ago, total in ((6, "180.00"), (3, "190.00")):
        db_session.add(
            PortfolioSnapshot(
                user_id=user.id,
                captured_at=now - timedelta(hours=hours_ago),
                total_value_usd=Decimal(total),
                holdings_count=1,
            )
        )
    await db_session.commit()

    hist = await portfolio_service.history(db_session, user, "1D")
    values = [p.price_usd for p in hist.points]

    # yesterday anchor + 2 intraday snapshots + live now
    assert len(hist.points) >= 4, values
    assert 180.0 in values and 190.0 in values, values
    # Terminal point is the canonical live total (grade-aware basis).
    assert values[-1] == 200.0
    # Series stays chronological.
    stamps = [p.date for p in hist.points]
    assert stamps == sorted(stamps)


@pytest.mark.anyio
async def test_history_read_captures_a_snapshot(db_session):
    user = await _mk_user(db_session)
    await _mk_holding(db_session, user, "75.00")

    before = (
        (
            await db_session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert before == []

    await portfolio_service.history(db_session, user, "1W")

    after = (
        (
            await db_session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(after) == 1
    assert float(after[0].total_value_usd) == 75.0


@pytest.mark.anyio
async def test_compaction_keeps_last_snapshot_per_day(db_session):
    """Rows older than the intraday window collapse to one (the day's last)."""
    user = await _mk_user(db_session)
    base = datetime.now(UTC) - timedelta(days=5)
    for hour, total in ((9, "100.00"), (13, "110.00"), (21, "120.00")):
        db_session.add(
            PortfolioSnapshot(
                user_id=user.id,
                captured_at=base.replace(hour=hour, minute=0),
                total_value_usd=Decimal(total),
                holdings_count=1,
            )
        )
    await db_session.commit()

    assert await portfolio_service.maybe_capture_snapshot(db_session, user, 130.0, 1)

    remaining = (
        (
            await db_session.execute(
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.user_id == user.id)
                .order_by(PortfolioSnapshot.captured_at.asc())
            )
        )
        .scalars()
        .all()
    )
    # 1 compacted row for the old day (the 21:00 = $120 one) + today's new one.
    assert len(remaining) == 2, [float(r.total_value_usd) for r in remaining]
    assert float(remaining[0].total_value_usd) == 120.0
    assert float(remaining[-1].total_value_usd) == 130.0


@pytest.mark.anyio
async def test_long_ranges_chart_real_snapshots(db_session):
    """1W buckets use REAL observed totals where snapshots exist (no more
    flat modeled line once tracking has begun)."""
    user = await _mk_user(db_session)
    await _mk_holding(db_session, user, "200.00")

    now = datetime.now(UTC)
    for days_ago, total in ((6, "150.00"), (4, "170.00"), (2, "185.00")):
        db_session.add(
            PortfolioSnapshot(
                user_id=user.id,
                captured_at=now - timedelta(days=days_ago),
                total_value_usd=Decimal(total),
                holdings_count=1,
            )
        )
    await db_session.commit()

    hist = await portfolio_service.history(db_session, user, "1W")
    values = [p.price_usd for p in hist.points]

    assert 150.0 in values and 170.0 in values and 185.0 in values, values
    # Terminal bucket still pinned to the live canonical total.
    assert values[-1] == 200.0
    # The week actually MOVES now.
    assert len(set(values)) >= 4, values
