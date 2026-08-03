"""Portfolio analytics — summary, history, and per-card sparklines.

Pure-DB, user-scoped. Every value is computed from the authenticated user's
real `graded_cards` and the price history embedded in each
`Card.card_metadata['price_history']` (populated by the `price_backfill`
worker). When data is absent we return empty/zero rather than fabricating
values; the UI is expected to render an empty state.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.models.sealed import SealedHolding
from app.models.user import User
from app.services.collection import collection_service, holding_valuation_service
from app.utils.logger import get_logger

_log = get_logger("portfolio")

PortfolioRange = Literal["1D", "1W", "1M", "3M", "YTD", "1Y", "ALL"]

# Sparklines are bounded to the newest N holdings — comfortably above the
# clients' largest visible page (mobile vault fetches 300) while keeping
# the endpoint's cost independent of vault size.
_SPARKLINE_MAX_ROWS = 500

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

# Per-range cap on how stale a carry-forward `price_history` point may
# be before we treat it as "no signal" for the bucket and fall back to
# the live value (which makes that bucket contribute 0 to the delta).
# Without this, a card whose only recorded price is months old gets
# its ancient price summed into yesterday's bucket while today's
# bucket uses the fresh live total — producing wildly inflated 1D
# deltas like "+347% today".
_RANGE_MAX_CARRY_DAYS: dict[str, int | None] = {
    "1D": 2,
    "1W": 7,
    "1M": 14,
    "3M": 30,
    "YTD": 30,
    "1Y": 60,
    "ALL": None,
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
    # ── Chart intelligence (additive; older clients simply ignore) ──────
    #: Highest / lowest rendered value across the series.
    high_usd: float | None = None
    low_usd: float | None = None
    #: Total recorded cost basis for the scope (None = nothing recorded) —
    #: lets the chart draw the break-even line without a second call.
    cost_basis_usd: float | None = None
    #: Biggest single-bucket rise / drop: {"date", "deltaUsd"}.
    best_day: dict | None = None
    worst_day: dict | None = None
    #: Which holdings drove the range's move, largest |Δ| first:
    #: [{gradeId, cardId, name, imageUrl, deltaUsd, deltaPct}].
    movers: list[dict] | None = None
    #: In-range acquisitions, newest first:
    #: [{date, count, valueUsd, name}].
    events: list[dict] | None = None

    def to_dict(self) -> dict:
        return {
            "range": self.range,
            "points": [p.to_dict() for p in self.points],
            "deltaUsd": self.delta_usd,
            "deltaPct": self.delta_pct,
            "highUsd": self.high_usd,
            "lowUsd": self.low_usd,
            "costBasisUsd": self.cost_basis_usd,
            "bestDay": self.best_day,
            "worstDay": self.worst_day,
            "movers": self.movers or [],
            "events": self.events or [],
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
    db: AsyncSession,
    user: User,
    collection_id: uuid.UUID | None = None,
    *,
    newest_first_limit: int | None = None,
) -> list[tuple[GradedCard, Card | None]]:
    """Load the user's holdings (+ catalog card) in one join.

    ``newest_first_limit`` bounds the fetch to the N most recently graded
    holdings — use it for per-row VISUALS (sparklines) where a bounded
    subset is fine. Leave it ``None`` for anything that must see every
    holding (the portfolio history total, for instance) — a bounded total
    would silently undercount large vaults.
    """
    where = [GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None)]
    scope = collection_service.holdings_scope(collection_id, user)
    if scope is not None:
        where.append(scope)
    stmt = (
        select(GradedCard, Card)
        .outerjoin(Card, Card.id == GradedCard.card_id)
        .where(*where)
    )
    if newest_first_limit is not None:
        stmt = stmt.order_by(GradedCard.graded_at.desc().nulls_last()).limit(
            newest_first_limit
        )
    rows = (await db.execute(stmt)).all()
    return [(g, c) for (g, c) in rows]


def current_market_value(card: Card | None) -> Decimal | None:
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


def holding_value_usd(grade: GradedCard) -> float:
    """Canonical per-holding "collection value" — the one valuation basis.

    The honest value of a *graded* card is its grade-aware estimate
    (``GradedCard.estimated_value_usd``): the value the scan pipeline / owner
    assigned to *this slab*. The raw catalog market price
    (``Card.card_metadata['pricing_summary']['market']``, and the
    ``price_history`` snapshotted from it) is grade-agnostic and systematically
    *undervalues* slabbed cards — a PSA 10 commands a large premium over the raw
    card — so it must **not** be the valuation basis for collection totals.

    Every endpoint that reports a collection total runs each holding through
    THIS function so the numbers agree to the cent:

    * ``GET /v1/grades/summary``   (:func:`summary`)
    * ``GET /v1/grades/history``   (:func:`history` — terminal point)
    * ``GET /v1/analytics/overview`` (``portfolio_overview_service``)

    The raw ``price_history`` is still consulted, but only to derive the
    *shape* of the value-over-time curve (relative price movement), never the
    absolute total. See :func:`history`.
    """
    return (
        float(grade.estimated_value_usd)
        if grade.estimated_value_usd is not None
        else 0.0
    )


# ── Portfolio snapshots — real intraday points for the 1D chart ─────────
# Card prices tick at most daily, so the 1D range used to be two buckets
# (yesterday close → live now): a flat line whenever the market hadn't
# moved. Every live-total computation now opportunistically persists a
# snapshot (write-on-read; no worker/scheduler dependency), and the 1D
# series splices those true observations between the anchors.

_SNAPSHOT_MIN_INTERVAL = timedelta(minutes=30)
# Long retention so snapshots become the REAL long-range series (1W…1Y):
# full intraday granularity for the last 48 h, compacted to one point per
# UTC day beyond that (see the compaction pass in maybe_capture_snapshot).
_SNAPSHOT_RETENTION = timedelta(days=400)
_SNAPSHOT_INTRADAY_WINDOW = timedelta(hours=48)


def _snapshot_scope(collection_id: uuid.UUID | None):
    """NULL-safe WHERE fragment isolating one snapshot series.

    Every scope — the whole-vault "All" (NULL) and each collection — charts
    only its OWN observations. Without this isolation a collection-sized
    total captured while browsing that collection would land in the All
    series (and vice versa), minting fake cliffs on the 1D chart.
    """
    if collection_id is None:
        return PortfolioSnapshot.collection_id.is_(None)
    return PortfolioSnapshot.collection_id == collection_id


async def maybe_capture_snapshot(
    db: AsyncSession,
    user: User,
    total_value_usd: float,
    holdings_count: int,
    collection_id: uuid.UUID | None = None,
) -> bool:
    """Persist the live canonical total, at most once per throttle window.

    Scoped: the total is recorded under *collection_id*'s own series (NULL
    = the whole vault), and the throttle/retention/compaction all operate
    within that series only.

    Best effort — a failed snapshot must never break the read path that
    triggered it. Also enforces the retention window so the table stays
    bounded without a vacuum job.
    """
    if holdings_count <= 0:
        return False
    try:
        now = datetime.now(UTC)
        scope = _snapshot_scope(collection_id)
        last = (
            await db.execute(
                select(func.max(PortfolioSnapshot.captured_at)).where(
                    PortfolioSnapshot.user_id == user.id, scope
                )
            )
        ).scalar()
        if last is not None:
            if last.tzinfo is None:  # SQLite returns naive datetimes
                last = last.replace(tzinfo=UTC)
            if now - last < _SNAPSHOT_MIN_INTERVAL:
                return False
        db.add(
            PortfolioSnapshot(
                user_id=user.id,
                collection_id=collection_id,
                captured_at=now,
                total_value_usd=Decimal(str(round(total_value_usd, 2))),
                holdings_count=holdings_count,
            )
        )
        await db.execute(
            delete(PortfolioSnapshot).where(
                PortfolioSnapshot.user_id == user.id,
                scope,
                PortfolioSnapshot.captured_at < now - _SNAPSHOT_RETENTION,
            )
        )
        # Compaction: beyond the intraday window keep only the LAST snapshot
        # of each UTC day, so the table stays bounded (~1 row/user/day) while
        # long ranges chart real observed values. Piggybacks on the throttled
        # write path, so it runs at most every 30 min per active user.
        cutoff = now - _SNAPSHOT_INTRADAY_WINDOW
        old_rows = (
            await db.execute(
                select(PortfolioSnapshot.id, PortfolioSnapshot.captured_at)
                .where(
                    PortfolioSnapshot.user_id == user.id,
                    scope,
                    PortfolioSnapshot.captured_at < cutoff,
                )
                .order_by(PortfolioSnapshot.captured_at.asc())
            )
        ).all()
        keep_per_day: dict[str, object] = {}
        for row_id, ts in old_rows:
            day = (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).date().isoformat()
            keep_per_day[day] = row_id  # ordered asc → ends on the day's last
        drop = [rid for rid, _ in old_rows if rid not in set(keep_per_day.values())]
        if drop:
            await db.execute(
                delete(PortfolioSnapshot).where(PortfolioSnapshot.id.in_(drop))
            )
        await db.commit()
        return True
    except Exception:
        _log.warning("portfolio snapshot capture failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass
        return False


async def _intraday_snapshots(
    db: AsyncSession,
    user: User,
    since: datetime,
    collection_id: uuid.UUID | None = None,
) -> list[tuple[datetime, float]]:
    """Chronological (captured_at, total) observations after *since*,
    limited to *collection_id*'s own series (NULL = the All series)."""
    rows = (
        await db.execute(
            select(PortfolioSnapshot.captured_at, PortfolioSnapshot.total_value_usd)
            .where(
                PortfolioSnapshot.user_id == user.id,
                _snapshot_scope(collection_id),
                PortfolioSnapshot.captured_at > since,
            )
            .order_by(PortfolioSnapshot.captured_at.asc())
        )
    ).all()
    out: list[tuple[datetime, float]] = []
    for ts, total in rows:
        # SQLite returns naive datetimes; treat them as UTC.
        aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
        out.append((aware, float(total)))
    return out


async def summary(
    db: AsyncSession, user: User, collection_id: uuid.UUID | None = None
) -> dict:
    """Aggregate the user's vault into a single hero card payload."""
    # Self-heal before aggregating. The total below is a SQL SUM over
    # ``estimated_value_usd``, so a holding with NULL there contributes $0 and
    # the vault reads low (or $0 outright) — which is exactly what a quick-add
    # produced before valuation-on-create existed. The daily price worker also
    # backfills, but a user opening the app should not have to wait a day to
    # see their own collection's worth.
    #
    # Bounded, idempotent, and NULL-only: it never touches a value the owner
    # set, and once a vault is healed the guard query matches nothing and this
    # costs one indexed lookup.
    await holding_valuation_service.backfill_missing_values(db, limit=500)

    where = [GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None)]
    scope = collection_service.holdings_scope(collection_id, user)
    if scope is not None:
        where.append(scope)

    (
        total_value,
        card_count,
        cost_sum,
        cost_count,
        cost_value_sum,
        grade_sum,
        grade_count,
        unique_card_count,
    ) = (
        await db.execute(
            select(
                func.coalesce(func.sum(GradedCard.estimated_value_usd), 0),
                func.count(GradedCard.id),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                GradedCard.purchase_price_usd.is_not(None),
                                GradedCard.purchase_price_usd,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (GradedCard.purchase_price_usd.is_not(None), 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                # Current value of ONLY the cost-basis cards, so P/L compares
                # like-for-like. Summing the whole vault against a partial
                # cost basis would count every no-cost card's value as gain.
                func.coalesce(
                    func.sum(
                        case(
                            (
                                GradedCard.purchase_price_usd.is_not(None),
                                GradedCard.estimated_value_usd,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(func.sum(GradedCard.grade), 0),
                func.coalesce(
                    func.sum(case((GradedCard.grade.is_not(None), 1), else_=0)),
                    0,
                ),
                func.count(func.distinct(GradedCard.card_id)),
            ).where(*where)
        )
    ).one()

    loupe_graded_count = (
        await db.execute(
            select(func.count())
            .select_from(GradedCard)
            .where(*where, GradedCard.house == GradeHouseEnum.loupe)
        )
    ).scalar() or 0

    set_rows = (
        await db.execute(
            select(CardSet.name)
            .join(Card, Card.set_id == CardSet.id)
            .join(GradedCard, GradedCard.card_id == Card.id)
            .where(*where)
            .where(CardSet.name.is_not(None))
            .distinct()
            .order_by(CardSet.name.asc())
        )
    ).all()
    available_sets = [r[0] for r in set_rows if r[0]]

    # Tags are the only field still scanned row-by-row — JSON arrays are
    # small and this runs once per vault open, not per filter keystroke.
    tags_seen: dict[str, str] = {}
    tag_rows = (await db.execute(select(GradedCard.tags).where(*where))).scalars().all()
    for tag_list in tag_rows:
        for raw_tag in tag_list or []:
            tag = str(raw_tag).strip()
            if tag:
                tags_seen.setdefault(tag.lower(), tag)

    total = Decimal(str(total_value))
    cost = Decimal(str(cost_sum))
    cost_count = int(cost_count or 0)
    card_count = int(card_count or 0)
    grade_count = int(grade_count or 0)
    grade_sum = Decimal(str(grade_sum))

    # Sealed-product rollup (qty x estimated value / purchase price) so the
    # BACKEND defines whether sealed counts toward "collection value" — the
    # clients render these fields instead of summing holdings themselves.
    # Sealed lives outside collections, so a scoped summary is cards-only
    # (zeros); opened boxes are excluded from the investment rollup.
    sealed_value = Decimal("0")
    sealed_cost = Decimal("0")
    sealed_count = 0
    if collection_id is None:
        sealed_value_sum, sealed_cost_sum, sealed_count = (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(
                            SealedHolding.estimated_value_usd * SealedHolding.quantity
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            SealedHolding.purchase_price_usd * SealedHolding.quantity
                        ),
                        0,
                    ),
                    func.count(SealedHolding.id),
                ).where(
                    SealedHolding.user_id == user.id,
                    SealedHolding.deleted_at.is_(None),
                    SealedHolding.opened_at.is_(None),
                )
            )
        ).one()
        sealed_value = Decimal(str(sealed_value_sum))
        sealed_cost = Decimal(str(sealed_cost_sum))
        sealed_count = int(sealed_count or 0)

    avg_grade = float(grade_sum / grade_count) if grade_count else None
    await maybe_capture_snapshot(
        db, user, float(total), card_count, collection_id=collection_id
    )
    if cost_count > 0:
        # P/L is value-vs-cost over the SAME population (cards with a
        # recorded purchase price) — never whole-vault value vs partial cost.
        cost_value = Decimal(str(cost_value_sum))
        pnl_usd: float | None = float(cost_value - cost)
        pnl_pct: float | None = (
            float((cost_value - cost) / cost * 100) if cost > 0 else 0.0
        )
        total_cost: float | None = float(cost)
    else:
        pnl_usd = None
        pnl_pct = None
        total_cost = None
    return {
        "totalValueUsd": float(total),
        "cardCount": card_count,
        "avgGrade": avg_grade,
        "avgAccuracy": None,
        "totalCostUsd": total_cost,
        "costBasisCardCount": cost_count,
        "unrealizedPnlUsd": pnl_usd,
        "unrealizedPnlPct": pnl_pct,
        "uniqueCardCount": int(unique_card_count or 0),
        "loupeGradedCount": int(loupe_graded_count),
        "availableSets": available_sets,
        "availableTags": sorted(tags_seen.values(), key=str.lower),
        # Sealed + combined — the canonical "collection value" definition.
        "sealedValueUsd": float(sealed_value),
        "sealedCostUsd": float(sealed_cost),
        "sealedHoldingCount": sealed_count,
        "combinedValueUsd": float(total + sealed_value),
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
    max_carry_days: int | None = None,
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

    ``max_carry_days`` caps how far back the most-recent point
    on-or-before *on* may be before it's considered too stale to
    represent the bucket's value. Stale points behave like an empty
    history: live_fallback is used instead. This prevents a single
    months-old recording from anchoring the "yesterday" bucket while
    the "today" bucket pulls fresh live prices, which would mint a
    huge fake intraday delta.
    """
    if not history:
        if live_fallback is not None:
            return live_fallback
        return float(estimated_value_usd or 0)
    last = None
    last_date: date | None = None
    for d, p in history:
        if d <= on:
            last = p
            last_date = d
        else:
            break
    if last is None:
        # History exists but its earliest point is AFTER `on`.
        # Extrapolate that earliest known price backward — it's a
        # real recorded data point, just from after the requested
        # date, so it's a more honest signal than today's live value.
        last = history[0][1]
    elif (
        max_carry_days is not None
        and last_date is not None
        and (on - last_date).days > max_carry_days
    ):
        if live_fallback is not None:
            return live_fallback
        return float(estimated_value_usd or 0)
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
    db: AsyncSession,
    user: User,
    range_: PortfolioRange,
    collection_id: uuid.UUID | None = None,
) -> PortfolioHistory:
    """Compute portfolio value over time from real per-card price history."""
    rows = await _load_grades_with_cards(db, user, collection_id)
    if not rows:
        return PortfolioHistory(range=range_, points=[], delta_usd=0.0, delta_pct=0.0)

    today = datetime.now(UTC).date()

    # Establish the earliest reference date for ALL range.
    earliest_dates: list[date] = []
    # Per card: (canonical_value, price_history, raw_ref_price_today).
    #   canonical_value  — the grade-aware `holding_value_usd` (the basis the
    #                       summary + overview endpoints also use).
    #   price_history    — raw catalog-price points; used ONLY to derive the
    #                       relative shape of the curve, not its level.
    #   raw_ref_price_today — the most recent recorded raw price, the
    #                       denominator that normalises the ratio so the
    #                       terminal bucket lands exactly on canonical_value.
    per_card_history: list[tuple[float, list[tuple[date, float]], float | None]] = []
    # Today's canonical total — the single source of truth for "collection
    # value". Pinning the terminal bucket to this sum is what makes the
    # Command Center hero, the Vault summary, and the Analytics overview all
    # report the same number. The per-card raw `price_history` only bends the
    # line between past buckets; it never sets the absolute level (the raw
    # catalog price is grade-agnostic and would undervalue slabs).
    canonical_total = 0.0
    cost_basis_total = 0.0
    has_cost_basis = False
    # Parallel context for the intelligence block (attribution + events):
    # per_card_history stays the hot bucket-loop shape.
    holdings_ctx: list[tuple[GradedCard, Card | None, float, list, float | None]] = []
    for g, c in rows:
        hist = _extract_price_history(c)
        if hist:
            earliest_dates.append(hist[0][0])
        if g.graded_at is not None:
            earliest_dates.append(g.graded_at.date())
        value = holding_value_usd(g)
        # Most recent recorded raw price (no staleness cap — it's just the
        # normalising denominator). None when the card has no price history,
        # in which case the card stays flat at its canonical value.
        raw_ref = _value_on(None, hist, today) if hist else None
        per_card_history.append((value, hist, raw_ref))
        holdings_ctx.append((g, c, value, hist, raw_ref))
        canonical_total += value
        if g.purchase_price_usd is not None:
            cost_basis_total += float(g.purchase_price_usd)
            has_cost_basis = True
    earliest = min(earliest_dates) if earliest_dates else today - timedelta(days=30)

    buckets = _bucket_dates(range_, earliest)
    max_carry_days = _RANGE_MAX_CARRY_DAYS.get(range_)

    # REAL observations first: the user's compacted daily snapshots. Where a
    # bucket has a fresh-enough snapshot, chart the value we actually
    # recorded (same canonical basis) instead of the raw-ratio model — every
    # range gets progressively truer the longer Loupe has been tracking.
    snap_rows = await _intraday_snapshots(
        db, user, datetime.now(UTC) - _SNAPSHOT_RETENTION, collection_id
    )
    snap_daily: dict[date, float] = {}
    for ts, v in snap_rows:
        snap_daily[ts.date()] = v  # asc order → last write of each day wins
    snap_days = sorted(snap_daily)

    def _snapshot_on(b: date) -> float | None:
        """Last real observation on-or-before *b*, capped for staleness."""
        chosen: date | None = None
        for d in snap_days:
            if d <= b:
                chosen = d
            else:
                break
        if chosen is None:
            return None
        staleness = (b - chosen).days
        if staleness > (max_carry_days if max_carry_days is not None else 7):
            return None
        return snap_daily[chosen]

    points: list[PortfolioPoint] = []
    for b in buckets:
        # The terminal bucket ("today") is pinned to the canonical total so
        # every collection-value surface agrees to the cent. Past buckets
        # scale each card's canonical value by its raw-price ratio vs. today,
        # so the line still reflects real market movement while staying in the
        # same (grade-aware) units as the terminal point.
        if b >= today:
            points.append(
                PortfolioPoint(date=b.isoformat(), price_usd=round(canonical_total, 2))
            )
            continue
        observed = _snapshot_on(b)
        if observed is not None:
            points.append(
                PortfolioPoint(date=b.isoformat(), price_usd=round(observed, 2))
            )
            continue
        total = 0.0
        for value, hist, raw_ref in per_card_history:
            if not hist or raw_ref is None or raw_ref <= 0:
                # No usable price signal → flat at the canonical value (this
                # card contributes 0 to the range's delta rather than a
                # fabricated move from the scan-time estimate gap).
                total += value
                continue
            # `_value_on` returns the raw price on-or-before `b`, falling back
            # to `raw_ref` when the bucket has no fresh point (no/stale
            # history) → ratio 1.0 → flat at the canonical value.
            raw_b = _value_on(
                None,
                hist,
                b,
                live_fallback=raw_ref,
                max_carry_days=max_carry_days,
            )
            total += value * (raw_b / raw_ref)
        points.append(PortfolioPoint(date=b.isoformat(), price_usd=round(total, 2)))

    # 1D: splice REAL intraday observations between the yesterday-close and
    # live-now anchors. Snapshot totals use the same canonical grade-aware
    # basis, so the curve stays in identical units. Timestamps are full ISO
    # datetimes (the anchors are date-only ISO / a datetime for "now"), and
    # the whole series is kept chronological.
    if range_ == "1D" and len(points) == 2:
        yesterday_anchor, live_anchor = points
        now_dt = datetime.now(UTC)
        since = datetime.combine(
            date.fromisoformat(yesterday_anchor.date), datetime.min.time(), tzinfo=UTC
        )
        snaps = await _intraday_snapshots(db, user, since, collection_id)
        if snaps:
            mid = [
                PortfolioPoint(date=ts.isoformat(), price_usd=round(v, 2))
                for ts, v in snaps
                if ts < now_dt
            ]
            points = [
                yesterday_anchor,
                *mid,
                PortfolioPoint(
                    date=now_dt.isoformat(), price_usd=live_anchor.price_usd
                ),
            ]

    # Opportunistic snapshot of the live canonical total (throttled inside).
    # Runs AFTER the 1D splice so the snapshot written by this request never
    # appears inside its own response.
    await maybe_capture_snapshot(
        db, user, canonical_total, len(rows), collection_id=collection_id
    )

    first = points[0].price_usd if points else 0.0
    last = points[-1].price_usd if points else 0.0
    delta_usd = round(last - first, 2)
    delta_pct = round((delta_usd / first * 100), 2) if first > 0 else 0.0

    # ── Chart intelligence — what Robinhood doesn't tell you ────────────
    # All derived from data this function already loaded; no extra queries.

    high_usd = round(max(p.price_usd for p in points), 2) if points else None
    low_usd = round(min(p.price_usd for p in points), 2) if points else None

    # Best/worst bucket-over-bucket move. Skipped for 1D (its points are
    # intraday observations — "best day" doesn't apply within a day).
    best_day: dict | None = None
    worst_day: dict | None = None
    if range_ != "1D" and len(points) >= 3:
        best = worst = None
        for prev, cur in itertools.pairwise(points):
            step = round(cur.price_usd - prev.price_usd, 2)
            if best is None or step > best[1]:
                best = (cur.date, step)
            if worst is None or step < worst[1]:
                worst = (cur.date, step)
        if best is not None and best[1] > 0:
            best_day = {"date": best[0], "deltaUsd": best[1]}
        if worst is not None and worst[1] < 0:
            worst_day = {"date": worst[0], "deltaUsd": worst[1]}

    # Attribution: each holding's contribution to the range move, using the
    # SAME ratio model as the curve itself (value x raw-price ratio), so the
    # per-card deltas are consistent with what the line shows.
    range_start = buckets[0] if buckets else today
    movers: list[dict] = []
    for g, c, value, hist, raw_ref in holdings_ctx:
        if not hist or raw_ref is None or raw_ref <= 0:
            continue  # flat by construction → contributed nothing
        raw_first = _value_on(
            None,
            hist,
            range_start,
            live_fallback=raw_ref,
            max_carry_days=max_carry_days,
        )
        card_delta = round(value - value * (raw_first / raw_ref), 2)
        if abs(card_delta) < 0.01:
            continue
        base = value * (raw_first / raw_ref)
        movers.append(
            {
                "gradeId": str(g.id),
                "cardId": str(g.card_id) if g.card_id else None,
                "name": c.name if c is not None else None,
                "imageUrl": c.image_url if c is not None else None,
                "deltaUsd": card_delta,
                "deltaPct": round(card_delta / base * 100, 2) if base > 0 else 0.0,
            }
        )
    movers.sort(key=lambda m: abs(m["deltaUsd"]), reverse=True)
    movers = movers[:3]

    # Acquisition markers: cards added inside the charted window, grouped by
    # day (newest first) so the chart can pin "you added N cards here" dots.
    events_by_day: dict[str, dict] = {}
    for g, c, value, _hist, _raw in holdings_ctx:
        if g.graded_at is None:
            continue
        d = g.graded_at.date()
        if d < range_start or d > today:
            continue
        key = d.isoformat()
        entry = events_by_day.setdefault(
            key,
            {"date": key, "count": 0, "valueUsd": 0.0, "name": None},
        )
        entry["count"] += 1
        entry["valueUsd"] = round(entry["valueUsd"] + value, 2)
        if entry["name"] is None and c is not None:
            entry["name"] = c.name
    events = sorted(events_by_day.values(), key=lambda e: e["date"], reverse=True)[:20]

    return PortfolioHistory(
        range=range_,
        points=points,
        delta_usd=delta_usd,
        delta_pct=delta_pct,
        high_usd=high_usd,
        low_usd=low_usd,
        cost_basis_usd=round(cost_basis_total, 2) if has_cost_basis else None,
        best_day=best_day,
        worst_day=worst_day,
        movers=movers,
        events=events,
    )


async def sparklines(
    db: AsyncSession,
    user: User,
    points: int = 14,
    collection_id: uuid.UUID | None = None,
) -> list[dict]:
    """Per-card 14-point trend pulled from real `price_history`."""
    # Sparklines are per-ROW visuals: the clients render at most a few
    # hundred rows (mobile vault pages at 300), so bounding the fetch to
    # the newest holdings keeps this endpoint O(bound) instead of
    # O(vault). Rows beyond the bound simply render without a sparkline.
    rows = await _load_grades_with_cards(
        db, user, collection_id, newest_first_limit=_SPARKLINE_MAX_ROWS
    )
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
    "current_market_value",
    "history",
    "holding_value_usd",
    "sparklines",
    "summary",
]
