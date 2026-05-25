"""Per-period portfolio aggregator used by the PDF report builder.

Given a (user, period_start, period_end) tuple, this module returns a
plain `ReportSnapshot` dataclass containing every number the renderer
needs:

* opening / closing portfolio value
* unrealized P/L over the period
* grade & TCG breakdowns
* top movers (winners + losers) inside the window
* a daily value series for the headline chart
* a per-card table of holdings as of the period close

All values are computed from the user's real `graded_cards` and the
embedded `Card.card_metadata['price_history']`. When data is missing we
return zeros / empty lists rather than fabricating numbers — the renderer
is responsible for rendering an honest "no data" panel.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.grade import GradedCard
from app.models.user import User
from app.services.collection.portfolio_service import _extract_price_history


@dataclass(slots=True)
class HoldingRow:
    card_id: str
    name: str
    set_name: str | None
    grade: float
    value_close_usd: float
    value_open_usd: float
    delta_pct: float


@dataclass(slots=True)
class MoverRow:
    card_id: str
    name: str
    set_name: str | None
    value_close_usd: float
    delta_usd: float
    delta_pct: float


@dataclass(slots=True)
class SeriesPoint:
    date: date
    value_usd: float


@dataclass(slots=True)
class ReportSnapshot:
    user_email: str
    period_label: str  # "May 2026" / "2026"
    period_start: date
    period_end: date

    card_count: int
    opening_value_usd: float
    closing_value_usd: float
    delta_usd: float
    delta_pct: float

    total_cost_usd: float | None
    unrealized_pnl_usd: float | None
    unrealized_pnl_pct: float | None

    avg_grade: float | None
    series: list[SeriesPoint] = field(default_factory=list)
    holdings: list[HoldingRow] = field(default_factory=list)
    top_gainers: list[MoverRow] = field(default_factory=list)
    top_losers: list[MoverRow] = field(default_factory=list)
    grade_buckets: list[tuple[str, int]] = field(default_factory=list)
    tcg_breakdown: list[tuple[str, float]] = field(default_factory=list)


def _value_on(
    estimated_value_usd: Decimal | None,
    history: list[tuple[date, float]],
    on: date,
) -> float:
    """Return the card's estimated USD value as of *on* using last-known-price."""
    if not history:
        return float(estimated_value_usd or 0)
    last: float | None = None
    for d, p in history:
        if d <= on:
            last = p
        else:
            break
    if last is None:
        last = history[0][1]
    return float(last)


async def build_snapshot(
    db: AsyncSession,
    *,
    user: User,
    period_label: str,
    period_start: date,
    period_end: date,
) -> ReportSnapshot:
    """Aggregate everything the renderer needs for one statement window."""
    rows = (
        await db.execute(
            select(GradedCard, Card, CardSet)
            .outerjoin(Card, Card.id == GradedCard.card_id)
            .outerjoin(CardSet, CardSet.id == Card.set_id)
            .where(
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
                # Only include cards that existed *during or before* the
                # window. Cards graded after the period close shouldn't
                # appear on a statement that covers an earlier window.
                GradedCard.graded_at <= _eod_dt(period_end),
            )
        )
    ).all()

    holdings: list[HoldingRow] = []
    gainers_pool: list[MoverRow] = []
    losers_pool: list[MoverRow] = []
    open_total = 0.0
    close_total = 0.0
    cost_total = Decimal("0")
    cost_count = 0
    grade_sum = Decimal("0")
    grade_count = 0
    grade_buckets: dict[str, int] = {}
    tcg_totals: dict[str, float] = {}

    for g, card, card_set in rows:
        history = _extract_price_history(card)
        v_open = _value_on(g.estimated_value_usd, history, period_start)
        v_close = _value_on(g.estimated_value_usd, history, period_end)
        open_total += v_open
        close_total += v_close

        if g.purchase_price_usd is not None:
            cost_total += g.purchase_price_usd
            cost_count += 1
        if g.grade is not None:
            grade_sum += g.grade
            grade_count += 1

        bucket = _grade_bucket(float(g.grade))
        grade_buckets[bucket] = grade_buckets.get(bucket, 0) + 1
        if card is not None:
            tcg_key = card.tcg.value if hasattr(card.tcg, "value") else str(card.tcg)
            tcg_totals[tcg_key] = tcg_totals.get(tcg_key, 0.0) + v_close

        delta_usd = v_close - v_open
        delta_pct = (delta_usd / v_open * 100.0) if v_open > 0 else 0.0
        name = card.name if card is not None else "Unknown card"
        set_name = card_set.name if card_set is not None else None

        holdings.append(
            HoldingRow(
                card_id=str(g.card_id),
                name=name,
                set_name=set_name,
                grade=float(g.grade),
                value_close_usd=v_close,
                value_open_usd=v_open,
                delta_pct=delta_pct,
            )
        )
        mover = MoverRow(
            card_id=str(g.card_id),
            name=name,
            set_name=set_name,
            value_close_usd=v_close,
            delta_usd=delta_usd,
            delta_pct=delta_pct,
        )
        # Only consider cards that actually had a price reference at both
        # endpoints — otherwise the "movement" is just metadata noise.
        if v_open > 0 and abs(delta_usd) > 0:
            if delta_usd >= 0:
                gainers_pool.append(mover)
            else:
                losers_pool.append(mover)

    holdings.sort(key=lambda h: h.value_close_usd, reverse=True)
    gainers_pool.sort(key=lambda m: m.delta_pct, reverse=True)
    losers_pool.sort(key=lambda m: m.delta_pct)

    series = _build_series(rows, period_start, period_end)

    delta_usd_total = close_total - open_total
    delta_pct_total = (delta_usd_total / open_total * 100.0) if open_total > 0 else 0.0

    if cost_count > 0:
        pnl_usd: float | None = float(Decimal(str(close_total)) - cost_total)
        pnl_pct: float | None = (
            float((Decimal(str(close_total)) - cost_total) / cost_total * 100)
            if cost_total > 0
            else 0.0
        )
        total_cost: float | None = float(cost_total)
    else:
        pnl_usd = None
        pnl_pct = None
        total_cost = None

    grade_order = ["10", "9-9.5", "8-8.5", "7-7.5", "6 & below"]
    grade_bucket_rows = [(k, grade_buckets.get(k, 0)) for k in grade_order]

    tcg_rows = sorted(tcg_totals.items(), key=lambda x: x[1], reverse=True)

    return ReportSnapshot(
        user_email=user.email,
        period_label=period_label,
        period_start=period_start,
        period_end=period_end,
        card_count=len(rows),
        opening_value_usd=open_total,
        closing_value_usd=close_total,
        delta_usd=delta_usd_total,
        delta_pct=delta_pct_total,
        total_cost_usd=total_cost,
        unrealized_pnl_usd=pnl_usd,
        unrealized_pnl_pct=pnl_pct,
        avg_grade=float(grade_sum / grade_count) if grade_count else None,
        series=series,
        holdings=holdings,
        top_gainers=gainers_pool[:5],
        top_losers=losers_pool[:5],
        grade_buckets=grade_bucket_rows,
        tcg_breakdown=tcg_rows,
    )


def _grade_bucket(grade: float) -> str:
    if grade >= 10:
        return "10"
    if grade >= 9:
        return "9-9.5"
    if grade >= 8:
        return "8-8.5"
    if grade >= 7:
        return "7-7.5"
    return "6 & below"


def _eod_dt(d: date):
    from datetime import UTC, datetime, time

    return datetime.combine(d, time.max, tzinfo=UTC)


def _build_series(
    rows: Sequence[Any],
    start: date,
    end: date,
) -> list[SeriesPoint]:
    """Daily total portfolio value across the window (max ~366 points)."""
    if start > end:
        return []
    # Cap the rendered series so a 1-year report doesn't try to put 365
    # ticks on a 6-inch-wide page — granular enough for the chart, cheap
    # to compute.
    span_days = (end - start).days
    if span_days <= 31:
        step = 1
    elif span_days <= 92:
        step = 2
    else:
        step = 7
    out: list[SeriesPoint] = []
    cur = start
    while cur <= end:
        total = 0.0
        for g, card, _set in rows:
            history = _extract_price_history(card)
            total += _value_on(g.estimated_value_usd, history, cur)
        out.append(SeriesPoint(date=cur, value_usd=total))
        cur = cur + timedelta(days=step)
    # Always include the exact end point for an honest closing value.
    if out and out[-1].date != end:
        total = 0.0
        for g, card, _set in rows:
            history = _extract_price_history(card)
            total += _value_on(g.estimated_value_usd, history, end)
        out.append(SeriesPoint(date=end, value_usd=total))
    return out


__all__ = [
    "HoldingRow",
    "MoverRow",
    "ReportSnapshot",
    "SeriesPoint",
    "build_snapshot",
]
