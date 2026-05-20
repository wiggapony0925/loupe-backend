"""Market-index cohort series — peer benchmarks for the portfolio chart.

The PSA-10 cohort index answers a simple question:

> "Is my collection outpacing what other Loupe users own in PSA 10?"

Construction:

1. Find every catalog `Card` that at least one user owns as a PSA-10
   `GradedCard` (house in {psa, loupe} and grade >= 10).
2. For each card, read its `metadata['price_history']` (populated by
   the daily `price_backfill` worker — same source the portfolio
   chart already uses).
3. For each bucket date in the requested range, average each card's
   "last-known price on-or-before bucket" → cohort spot price.
4. Normalize the whole series so the first bucket = 100. The chart
   overlay assumes this normalization so the user can lay it on top
   of their own portfolio (which is normalized the same way at
   render time) and read off relative performance directly.

No authentication required — it's an aggregate market signal, not a
per-user view. Empty list when no cohort cards exist or none have any
price history yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card
from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from app.services.portfolio_service import (
    PortfolioRange,
    _bucket_dates,
    _extract_price_history,
    _value_on,
)
from app.utils.logger import get_logger

_log = get_logger("market_index")

# Houses we count as PSA-10-equivalent. `loupe` is our own grading; we
# include it because we trust our pipeline and treating it as a separate
# cohort would create a needlessly noisy second series.
_PSA10_HOUSES = (GradeHouseEnum.psa, GradeHouseEnum.loupe)
_PSA10_MIN_GRADE = Decimal("10.0")


@dataclass(slots=True)
class IndexPoint:
    date: str
    index_value: float

    def to_dict(self) -> dict:
        return {"date": self.date, "indexValue": self.index_value}


@dataclass(slots=True)
class MarketIndex:
    index_id: str
    range: PortfolioRange
    points: list[IndexPoint]
    delta_pct: float
    cohort_size: int

    def to_dict(self) -> dict:
        return {
            "indexId": self.index_id,
            "range": self.range,
            "points": [p.to_dict() for p in self.points],
            "deltaPct": self.delta_pct,
            "cohortSize": self.cohort_size,
        }


async def _load_psa10_cohort(db: AsyncSession) -> list[Card]:
    """Distinct catalog cards held by at least one user as PSA-10."""
    stmt = (
        select(Card)
        .join(GradedCard, GradedCard.card_id == Card.id)
        .where(
            GradedCard.house.in_(_PSA10_HOUSES),
            GradedCard.grade >= _PSA10_MIN_GRADE,
            GradedCard.deleted_at.is_(None),
        )
        .distinct()
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def psa10_index(db: AsyncSession, range_: PortfolioRange) -> MarketIndex:
    """Compute the PSA-10 popularity index (normalized to 100)."""
    cards = await _load_psa10_cohort(db)
    cohort_size = len(cards)
    if cohort_size == 0:
        return MarketIndex(
            index_id="psa10",
            range=range_,
            points=[],
            delta_pct=0.0,
            cohort_size=0,
        )

    histories: list[list[tuple[date, float]]] = []
    earliest_dates: list[date] = []
    for c in cards:
        hist = _extract_price_history(c)
        if not hist:
            continue
        histories.append(hist)
        earliest_dates.append(hist[0][0])

    if not histories:
        return MarketIndex(
            index_id="psa10",
            range=range_,
            points=[],
            delta_pct=0.0,
            cohort_size=cohort_size,
        )

    earliest = min(earliest_dates)
    buckets = _bucket_dates(range_, earliest)

    # Per-bucket cohort spot price = arithmetic mean across cards of the
    # last-known price on-or-before the bucket. Cards with no history at
    # all are skipped (filtered above), so the mean is never weighted by
    # zero-valued ghosts.
    spot_series: list[float] = []
    for b in buckets:
        total = 0.0
        n = 0
        for hist in histories:
            v = _value_on(None, hist, b)
            if v > 0:
                total += v
                n += 1
        spot_series.append(total / n if n > 0 else 0.0)

    # Normalize to 100 at the first non-zero bucket. If the entire series
    # is zero (cohort cards exist but no price history overlaps the
    # requested range) we emit an empty list rather than a flat 100 line
    # — the chart should hide the overlay in that case.
    base = next((v for v in spot_series if v > 0), 0.0)
    if base == 0.0:
        return MarketIndex(
            index_id="psa10",
            range=range_,
            points=[],
            delta_pct=0.0,
            cohort_size=cohort_size,
        )

    points = [
        IndexPoint(date=b.isoformat(), index_value=round((v / base) * 100.0, 2))
        for b, v in zip(buckets, spot_series, strict=False)
    ]
    last = points[-1].index_value if points else 100.0
    delta_pct = round(last - 100.0, 2)
    return MarketIndex(
        index_id="psa10",
        range=range_,
        points=points,
        delta_pct=delta_pct,
        cohort_size=cohort_size,
    )


__all__ = ["IndexPoint", "MarketIndex", "psa10_index"]


# Suppress unused-import lints — `timedelta` is intentionally kept
# imported for downstream cohort helpers.
_ = timedelta
