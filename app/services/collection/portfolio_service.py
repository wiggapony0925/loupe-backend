"""Portfolio analytics — summary, history, and per-card sparklines.

Pure-DB, user-scoped. Every value is computed from the authenticated user's
real `graded_cards` and the price history embedded in each
`Card.card_metadata['price_history']` (populated by the `price_backfill`
worker). When data is absent we return empty/zero rather than fabricating
values; the UI is expected to render an empty state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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


def _current_market_value(card: Card | None) -> Decimal | None:
    """Pull the latest known market price from ``Card.card_metadata``.

    Returns the live ``pricing_summary.market.amount`` when present so
    the vault total tracks today's price instead of the value frozen
    into ``GradedCard.estimated_value_usd`` at scan time. Returns
    ``None`` when no fresh price is available — callers should fall
    back to the stored estimate.
    """
    if card is None or not isinstance(card.card_metadata, dict):
        return None
    pricing = card.card_metadata.get("pricing_summary")
    if not isinstance(pricing, dict):
        return None
    market = pricing.get("market")
    if not isinstance(market, dict):
        return None
    amount = market.get("amount")
    if amount is None:
        return None
    try:
        return Decimal(str(amount))
    except (ValueError, ArithmeticError):
        return None


async def summary(db: AsyncSession, user: User) -> dict:
    """Aggregate the user's vault into a single hero card payload."""
    rows = await _load_grades_with_cards(db, user)
    total = Decimal("0")
    cost = Decimal("0")
    cost_count = 0
    grade_sum = Decimal("0")
    grade_count = 0
    # Vault-shape extras: lets the client render the Vault page header
    # (Holdings / Avg Grade / Loupe-graded pills + the Category chip
    # row) without downloading the full collection just to compute
    # `Set(c.set)` and `count(c.house == 'loupe')` in JS.
    unique_card_ids: set = set()
    loupe_graded_count = 0
    for g, card in rows:
        # Prefer the live market price from the linked Card row so the
        # vault total tracks today's market instead of the snapshot
        # captured at scan time. Fall back to the stored estimate when
        # no fresh price exists yet.
        live = _current_market_value(card)
        if live is not None:
            total += live
        elif g.estimated_value_usd is not None:
            total += g.estimated_value_usd
        if g.purchase_price_usd is not None:
            cost += g.purchase_price_usd
            cost_count += 1
        if g.grade is not None:
            grade_sum += g.grade
            grade_count += 1
        if g.card_id is not None:
            unique_card_ids.add(g.card_id)
        house_val = (
            g.house.value if hasattr(g.house, "value") else str(g.house or "")
        ).lower()
        if house_val == "loupe":
            loupe_graded_count += 1
    # Distinct set names the user owns. Pulled in a single SQL pass
    # (graded_cards → cards → card_sets) so the cost is one query, not
    # one per row. Result is sorted for stable client-side rendering.
    from app.models.card import CardSet

    set_rows = (
        await db.execute(
            select(CardSet.name)
            .join(Card, Card.set_id == CardSet.id)
            .join(GradedCard, GradedCard.card_id == Card.id)
            .where(GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None))
            .where(CardSet.name.is_not(None))
            .distinct()
            .order_by(CardSet.name.asc())
        )
    ).all()
    available_sets = [r[0] for r in set_rows if r[0]]
    avg_grade = float(grade_sum / grade_count) if grade_count else None
    # Unrealized P/L is `value - cost`, but only meaningful when the user
    # has recorded a cost on at least one card. We report `null` (not 0)
    # in that case so the UI can hide the P/L chip instead of showing
    # "+$0.00 (+0%)", which would mislead a brand-new collector.
    if cost_count > 0:
        pnl_usd: float | None = float(total - cost)
        pnl_pct: float | None = float((total - cost) / cost * 100) if cost > 0 else 0.0
        total_cost: float | None = float(cost)
    else:
        pnl_usd = None
        pnl_pct = None
        total_cost = None
    return {
        "totalValueUsd": float(total),
        "cardCount": len(rows),
        # Average grade (0-10) is the most honest "quality" signal we have
        # until the scan pipeline reports per-job accuracy. Frontend shows
        # null as "—" rather than fabricating an accuracy percentage.
        "avgGrade": avg_grade,
        "avgAccuracy": None,
        # Cost basis & unrealized P/L. `null` means the user has not
        # recorded a purchase price on any card yet.
        "totalCostUsd": total_cost,
        "costBasisCardCount": cost_count,
        "unrealizedPnlUsd": pnl_usd,
        "unrealizedPnlPct": pnl_pct,
        # Vault aggregates — moved off the client so the Vault header
        # never needs the full card list to render.
        "uniqueCardCount": len(unique_card_ids),
        "loupeGradedCount": loupe_graded_count,
        "availableSets": available_sets,
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
            p = float(raw_price)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            continue
        out.append((d, p))
    out.sort(key=lambda x: x[0])
    return out


def _value_on(
    estimated_value_usd: Decimal | None,
    history: list[tuple[date, float]],
    on: date,
    live_fallback: float | None = None,
) -> float:
    """Return the card's estimated value as of *on* using last-known-price.

    When *history* contains no point on-or-before *on* we can't honestly
    say what the card was worth back then. Two fallbacks are tried in
    order:

    1. ``live_fallback`` (today's live market price). This is the
       correct behaviour for delta calculations — a card with no
       historical pricing contributes **zero** to a range's delta
       because we treat it as flat at today's value rather than
       fabricating a starting price from the scan-time estimate.
    2. ``estimated_value_usd`` (the user's scan-time estimate). Only
       used when no live market price is available either, so a card
       still appears on the historical curve at *some* value.

    If even that is missing we fall through to the earliest known
    price point. The function never returns ``None``.
    """
    if not history:
        if live_fallback is not None:
            return live_fallback
        return float(estimated_value_usd or 0)
    last = None
    for d, p in history:
        if d <= on:
            last = p
        else:
            break
    if last is None:
        # History exists but its earliest point is AFTER `on`.
        # Extrapolate that earliest known price backward — it's a
        # real recorded data point, just from after the requested
        # date, so it's a more honest signal than today's live value.
        last = history[0][1]
    return float(last)


def _bucket_dates(range_: PortfolioRange, earliest: date) -> list[date]:
    """Return the bucket end-dates for *range_*, oldest → newest."""
    now = datetime.now(UTC).date()
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
        # 1D: we don't have intraday tick data, but the per-card
        # `price_history` table is keyed by day, so we can still surface
        # a real day-over-day delta. Emit yesterday + today. The caller
        # values the "today" bucket from the live market total (so the
        # Command Center hero matches the Vault summary to the cent),
        # and the "yesterday" bucket from each card's last-known price
        # on-or-before yesterday — giving us the "+$X.XX since
        # yesterday's close" semantics Robinhood/Collectr users expect
        # without backfilling minute-level snapshots.
        yesterday = now - timedelta(days=1)
        return [yesterday, now]
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
    # (estimated_value_usd, price_history, live_today_value_for_this_card)
    per_card_history: list[
        tuple[Decimal | None, list[tuple[date, float]], float]
    ] = []
    # Today's live total — reuses the same per-card market lookup the
    # vault summary endpoint uses so the Command Center hero value and
    # the Vault portfolio value always agree. Without this, history's
    # last bucket sums the synthetic `price_history` while summary sums
    # `pricing_summary.market.amount`, and the two screens show
    # different numbers for the same portfolio.
    live_today_total = 0.0
    for g, c in rows:
        hist = _extract_price_history(c)
        if hist:
            earliest_dates.append(hist[0][0])
        if g.graded_at is not None:
            earliest_dates.append(g.graded_at.date())
        live = _current_market_value(c)
        if live is not None:
            live_value = float(live)
        elif g.estimated_value_usd is not None:
            live_value = float(g.estimated_value_usd)
        else:
            live_value = 0.0
        per_card_history.append((g.estimated_value_usd, hist, live_value))
        live_today_total += live_value
    earliest = (
        min(earliest_dates)
        if earliest_dates
        else datetime.now(UTC).date() - timedelta(days=30)
    )

    buckets = _bucket_dates(range_, earliest)
    points: list[PortfolioPoint] = []
    today = datetime.now(UTC).date()
    for b in buckets:
        # The terminal bucket ("today") is pinned to the same live total
        # the vault summary returns so both screens agree to the cent.
        # Past buckets still use the per-card recorded history so the
        # line shape reflects actual price movement.
        if b >= today:
            points.append(
                PortfolioPoint(date=b.isoformat(), price_usd=round(live_today_total, 2))
            )
            continue
        total = 0.0
        for est, hist, live_value in per_card_history:
            total += _value_on(est, hist, b, live_fallback=live_value)
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
                CardSparkline(card_id=str(g.id), points=pts, delta_pct=0.0).to_dict()
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
