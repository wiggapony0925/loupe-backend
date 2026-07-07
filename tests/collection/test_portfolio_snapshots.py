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


async def _mk_user(db) -> User:  # noqa: ANN001 - project fixture session
    user = User(
        id=uuid.uuid4(),
        email=f"snap-{uuid.uuid4().hex[:8]}@test.dev",
        display_name="Snap",
    )
    db.add(user)
    await db.commit()
    return user


async def _mk_holding(db, user: User, value: str) -> GradedCard:  # noqa: ANN001
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
async def test_capture_is_throttled(db_session):  # noqa: ANN001
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
async def test_capture_skips_empty_vaults(db_session):  # noqa: ANN001
    user = await _mk_user(db_session)
    captured = await portfolio_service.maybe_capture_snapshot(db_session, user, 0.0, 0)
    assert captured is False


@pytest.mark.anyio
async def test_retention_purges_old_rows(db_session):  # noqa: ANN001
    user = await _mk_user(db_session)
    stale = PortfolioSnapshot(
        user_id=user.id,
        captured_at=datetime.now(UTC) - timedelta(days=30),
        total_value_usd=Decimal("50.00"),
        holdings_count=1,
    )
    db_session.add(stale)
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
    assert len(remaining) == 1, "30-day-old snapshot must be purged"
    assert float(remaining[0].total_value_usd) == 120.0


@pytest.mark.anyio
async def test_1d_history_splices_intraday_snapshots(db_session):  # noqa: ANN001
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
async def test_history_read_captures_a_snapshot(db_session):  # noqa: ANN001
    user = await _mk_user(db_session)
    await _mk_holding(db_session, user, "75.00")

    before = (
        await db_session.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user.id)
        )
    ).scalars().all()
    assert before == []

    await portfolio_service.history(db_session, user, "1W")

    after = (
        await db_session.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user.id)
        )
    ).scalars().all()
    assert len(after) == 1
    assert float(after[0].total_value_usd) == 75.0
