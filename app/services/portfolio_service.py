"""Portfolio analytics — summary, history, and per-card sparklines.

Pure-DB, user-scoped. Every value is computed from the authenticated user's
real `graded_cards` and the price history embedded in each
`Card.card_metadata['price_history']` (populated by the `price_backfill`
worker). When data is absent we return empty/zero rather than fabricating
values; the UI is expected to render an empty state.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card
from app.models.grade import GradedCard
from app.models.user import User
from app.utils.logger import get_logger

_log = get_logger("portfolio")

PortfolioRange = Literal["1D", "1W", "1M", "3M", "YTD", "1Y", "ALL"]

_RANGE_BUCKETS: dict[str, tuple[timedelta | None, str]] = {
    # delta_from_now, bucket_granularity
    "1D": (timedelta(days=1), "hour"),
    "1W": (timedelta(days=7), "day"),
    "1M": (timedelta(days=30), "day"),
    "3M": (timedelta(days=90), "day"),
    "YTD": (None, "day"),  # special-cased
    "1Y": (timedelta(days=365), "week"),
    "ALL": (None, "month"),  # no lower bound
}


@dataclass(slots=True)
class PortfolioPoint:
    date: str  # ISO date (UTC)
    price_usd: float

    def to_dict(self) -> dict:
        return {"date": self.date, "priceUsd": self.price_usd}


@dataclass(slots=True)
class PortfolioHistory:
    range: PortfolioRange
    points: list[PortfolioPoint]
    delta_usd: float
    delta_pct: float

    def to_dict(self) -> dict:
        return {
            "range": self.range,
            "points": [p.to_dict() for p in self.points],
            "deltaUsd": self.delta_usd,
            "deltaPct": self.delta_pct,
        }


@dataclass(slots=True)
class CardSparkline:
    card_id: str
    points: list[float]
    delta_pct: float

    def to_dict(self) -> dict:
        return {
            "cardId": self.card_id,
            "points": self.points,
            "deltaPct": self.delta_pct,
        }


async def _load_grades_with_cards(
    db: AsyncSession, user: User
) -> list[tuple[GradedCard, Card | None]]:
    rows = (
        await db.execute(
            select(GradedCard, Card)
            .outerjoin(Card, Card.id == GradedCard.card_id)
            .where(GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None))
        )
    ).all()
    return [(g, c) for (g, c) in rows]


async def summary(db: AsyncSession, user: User) -> dict:
    """Aggregate the user's vault into a single hero card payload."""
    rows = await _load_grades_with_cards(db, user)
    total = Decimal("0")
    grade_sum = Decimal("0")
    grade_count = 0
    for g, _card in rows:
        if g.estimated_value_usd is not None:
            total += g.estimated_value_usd
        if g.grade is not None:
            grade_sum += g.grade
            grade_count += 1
    avg_grade = float(grade_sum / grade_count) if grade_count else None
    return {
        "totalValueUsd": float(total),
        "cardCount": len(rows),
        # Average grade (0–10) is the most honest "quality" signal we have
        # until the scan pipeline reports per-job accuracy. Frontend shows
        # null as "—" rather than fabricating an accuracy percentage.
        "avgGrade": avg_grade,
        "avgAccuracy": None,
    }


def _extract_price_history(card: Card | None) -> list[tuple[date, float]]:
    """Pull `(date, priceUsd)` pairs out of `Card.card_metadata.price_history`.

    Tolerates the various shapes the worker may write (list of dicts with
    `date`/`price`/`priceUsd` keys, or a flat dict keyed by ISO date).
    """
    if card is None or not card.card_metadata:
        return []
    meta = card.card_metadata
    history = meta.get("price_history") if isinstance(meta, dict) else None
    if not history:
        return []
    out: list[tuple[date, float]] = []
    items: list[tuple[object, object]] = []
    if isinstance(history, dict):
        items = list(history.items())
    elif isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            items.append(
                (entry.get("date"), entry.get("priceUsd") or entry.get("price"))
            )
    else:
        return []
    for raw_date, raw_price in items:
        if raw_date is None or raw_price is None:
            continue
        try:
            d = date.fromisoformat(str(raw_date)[:10])
            p = float(raw_price)
        except (ValueError, TypeError):
            continue
        out.append((d, p))
    out.sort(key=lambda x: x[0])
    return out


def _value_on(
    estimated_value_usd: Decimal | None,
    history: list[tuple[date, float]],
    on: date,
) -> float:
    """Return the card's estimated value as of *on* using last-known-price."""
    if not history:
        return float(estimated_value_usd or 0)
    # last point on-or-before `on`
    last = None
    for d, p in history:
        if d <= on:
            last = p
        else:
            break
    if last is None:
        # all history is after the requested date — fall back to first known
        last = history[0][1]
    return float(last)


def _bucket_dates(range_: PortfolioRange, earliest: date) -> list[date]:
    """Return the bucket end-dates for *range_*, oldest → newest."""
    now = datetime.now(timezone.utc).date()
    delta, granularity = _RANGE_BUCKETS[range_]
    if range_ == "YTD":
        start = date(now.year, 1, 1)
    elif range_ == "ALL":
        start = earliest
    else:
        assert delta is not None
        start = now - delta
    if start > now:
        start = now
    out: list[date] = []
    cur = start
    if granularity == "hour":
        # 1D bucketed by hour: emit one point per hour for the last 24h.
        # We don't actually have intraday price data, so for 1D we emit
        # *now* twice with the same value — the frontend renders it flat,
        # which is the honest representation.
        return [now, now]
    if granularity == "day":
        step = timedelta(days=1)
    elif granularity == "week":
        step = timedelta(days=7)
    else:  # month
        step = timedelta(days=30)
    while cur <= now:
        out.append(cur)
        cur = cur + step
    if not out or out[-1] != now:
        out.append(now)
    return out


async def history(
    db: AsyncSession, user: User, range_: PortfolioRange
) -> PortfolioHistory:
    """Compute portfolio value over time from real per-card price history."""
    rows = await _load_grades_with_cards(db, user)
    if not rows:
        return PortfolioHistory(range=range_, points=[], delta_usd=0.0, delta_pct=0.0)

    # Establish the earliest reference date for ALL range.
    earliest_dates: list[date] = []
    per_card_history: list[tuple[Decimal | None, list[tuple[date, float]]]] = []
    for g, c in rows:
        hist = _extract_price_history(c)
        if hist:
            earliest_dates.append(hist[0][0])
        if g.graded_at is not None:
            earliest_dates.append(g.graded_at.date())
        per_card_history.append((g.estimated_value_usd, hist))
    earliest = (
        min(earliest_dates)
        if earliest_dates
        else datetime.now(timezone.utc).date() - timedelta(days=30)
    )

    buckets = _bucket_dates(range_, earliest)
    points: list[PortfolioPoint] = []
    for b in buckets:
        total = 0.0
        for est, hist in per_card_history:
            total += _value_on(est, hist, b)
        points.append(PortfolioPoint(date=b.isoformat(), price_usd=round(total, 2)))

    first = points[0].price_usd if points else 0.0
    last = points[-1].price_usd if points else 0.0
    delta_usd = round(last - first, 2)
    delta_pct = round((delta_usd / first * 100), 2) if first > 0 else 0.0
    return PortfolioHistory(
        range=range_, points=points, delta_usd=delta_usd, delta_pct=delta_pct
    )


async def sparklines(db: AsyncSession, user: User, points: int = 14) -> list[dict]:
    """Per-card 14-point trend pulled from real `price_history`."""
    rows = await _load_grades_with_cards(db, user)
    out: list[dict] = []
    for g, c in rows:
        hist = _extract_price_history(c)
        if not hist:
            # No real history — emit a flat line at the current estimate so
            # the UI can still render the card row without fabricating motion.
            current = float(g.estimated_value_usd or 0)
            pts = [current] * points
            out.append(
                CardSparkline(
                    card_id=str(g.id), points=pts, delta_pct=0.0
                ).to_dict()
            )
            continue
        # Down-sample the real history to exactly `points` points.
        if len(hist) >= points:
            stride = len(hist) / points
            sampled = [hist[int(i * stride)][1] for i in range(points)]
        else:
            # Pad on the left with the first known price.
            pad = [hist[0][1]] * (points - len(hist))
            sampled = pad + [p for _, p in hist]
        first = sampled[0]
        last = sampled[-1]
        delta_pct = round(((last - first) / first * 100), 2) if first > 0 else 0.0
        out.append(
            CardSparkline(
                card_id=str(g.id),
                points=[round(p, 2) for p in sampled],
                delta_pct=delta_pct,
            ).to_dict()
        )
    return out


__all__ = [
    "CardSparkline",
    "PortfolioHistory",
    "PortfolioPoint",
    "history",
    "sparklines",
    "summary",
]
