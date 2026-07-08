"""Portfolio overview — single-shot analytics rollup for the Analytics tab.

Returns everything the mobile Analytics screen renders in **one** authenticated
request, so the client doesn't have to download the entire collection and
recompute aggregates on every render. Mirrors the design of the home-feed
service: pure-DB, no N+1, no synthetic data.

Response shape (see ``router/analytics/portfolio_overview.py`` for the
endpoint):

    {
      "stats":             { holdings, uniqueSets, avgGrade, gemRatePct,
                              avgValueUsd, oldestYear, totalValueUsd },
      "kpis":              { totalScans, avgGrade, gemRatePct, lastScanAt,
                              graderSplit: { psa, bgs, cgc } },
      "setIndexes":        [{ setName, count, totalValueUsd, sharePct,
                              changePct1y }],
      "movers":            { gainers: [...], losers: [...] },     // 3 each
      "concentration":     { top1Pct, top3Pct, top5Pct, top1: {cardName, valueUsd} },
      "yearDistribution":  [{ decade, count, valueUsd }],
      "gradeDistribution": [{ bucket, count }]
    }
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.grade import GradedCard
from app.models.user import User
from app.services.analytics.home_feed_service import _change_pct_1y
from app.services.collection import collection_service
from app.services.collection.portfolio_service import holding_value_usd

# ─── Helpers ──────────────────────────────────────────────────────────────


def _grader_key(house: Any) -> str | None:
    """Coerce the GradingHouse enum/string into one of the 3 UI keys."""
    raw = (house.value if hasattr(house, "value") else str(house or "")).lower()
    if raw in {"psa", "bgs", "cgc"}:
        return raw
    return None


def _grade_bucket(grade: float) -> str:
    """Bucket a numeric grade into the histogram bins the UI renders."""
    if grade >= 10:
        return "10"
    if grade >= 9.5:
        return "9.5"
    if grade >= 9:
        return "9"
    if grade >= 8:
        return "8"
    if grade >= 7:
        return "7"
    return "≤6"


_GRADE_BUCKET_ORDER = ["10", "9.5", "9", "8", "7", "≤6"]


# ─── Builder ──────────────────────────────────────────────────────────────


async def build_overview(
    db: AsyncSession, user: User, collection_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Compute every analytics widget the Analytics tab needs in one query."""
    where = [GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None)]
    scope = collection_service.holdings_scope(collection_id, user)
    if scope is not None:
        where.append(scope)
    rows = (
        await db.execute(
            select(GradedCard, Card, CardSet)
            .outerjoin(Card, Card.id == GradedCard.card_id)
            .outerjoin(CardSet, CardSet.id == Card.set_id)
            .where(*where)
        )
    ).all()

    # --- empty state -----------------------------------------------------
    if not rows:
        return {
            "stats": {
                "holdings": 0,
                "uniqueSets": 0,
                "avgGrade": 0.0,
                "gemRatePct": 0.0,
                "avgValueUsd": 0.0,
                "oldestYear": None,
                "totalValueUsd": 0.0,
            },
            "kpis": {
                "totalScans": 0,
                "avgGrade": 0.0,
                "gemRatePct": 0.0,
                "lastScanAt": None,
                "graderSplit": {"psa": 0, "bgs": 0, "cgc": 0},
            },
            "setIndexes": [],
            "movers": {"gainers": [], "losers": []},
            "concentration": None,
            "yearDistribution": [],
            "gradeDistribution": [
                {"bucket": b, "count": 0} for b in _GRADE_BUCKET_ORDER
            ],
        }

    # --- one pass: collect raw rows + per-card facts ---------------------
    holdings: list[dict[str, Any]] = []
    grade_sum = 0.0
    grade_count = 0
    gem_count = 0
    grader_split = {"psa": 0, "bgs": 0, "cgc": 0}
    last_scan_at = None
    year_min: int | None = None
    grade_buckets: dict[str, int] = dict.fromkeys(_GRADE_BUCKET_ORDER, 0)

    for g, c, s in rows:
        # `holding_value_usd` is the single canonical valuation basis shared
        # with the summary + history endpoints, so `stats.totalValueUsd` here
        # equals the history endpoint's terminal point to the cent.
        value = holding_value_usd(g)
        grade = float(g.grade) if g.grade is not None else None
        set_name = (s.name if s is not None else None) or (
            c.set.name
            if c is not None and getattr(c, "set", None) is not None
            else None
        )
        year = c.year if c is not None and c.year is not None else None
        holdings.append(
            {
                "gradeId": str(g.id),
                "cardId": str(g.card_id) if g.card_id is not None else None,
                "cardName": c.name if c is not None else None,
                "cardImageUrl": c.image_url if c is not None else None,
                "setName": set_name,
                "valueUsd": value,
                "grade": grade,
                "year": year,
                "changePct1y": _change_pct_1y(c, value),
            }
        )

        # KPI aggregates
        if grade is not None:
            grade_sum += grade
            grade_count += 1
            if grade >= 9.5:
                gem_count += 1
            grade_buckets[_grade_bucket(grade)] += 1
        gk = _grader_key(g.house)
        if gk is not None:
            grader_split[gk] += 1
        if g.graded_at is not None:
            iso = g.graded_at.isoformat()
            if last_scan_at is None or iso > last_scan_at:
                last_scan_at = iso
        if year is not None and (year_min is None or year < year_min):
            year_min = year

    total_count = len(holdings)
    total_value = sum(h["valueUsd"] for h in holdings)
    avg_grade = grade_sum / grade_count if grade_count else 0.0
    gem_rate_pct = (gem_count / total_count) * 100 if total_count else 0.0

    # --- Set indexes (group by setName) ----------------------------------
    set_groups: dict[str, list[dict[str, Any]]] = {}
    for h in holdings:
        name = h["setName"] or "Unknown set"
        set_groups.setdefault(name, []).append(h)

    set_indexes: list[dict[str, Any]] = []
    for name, items in set_groups.items():
        sub_total = sum(i["valueUsd"] for i in items)
        # Value-weighted average of card-level changePct1y; cards with
        # null pct are excluded from the average rather than treated as 0.
        weighted_sum = 0.0
        weight = 0.0
        for i in items:
            if i["changePct1y"] is not None and i["valueUsd"] > 0:
                weighted_sum += i["changePct1y"] * i["valueUsd"]
                weight += i["valueUsd"]
        change_pct = round(weighted_sum / weight, 2) if weight > 0 else None
        set_indexes.append(
            {
                "setName": name,
                "count": len(items),
                "totalValueUsd": round(sub_total, 2),
                "sharePct": round((sub_total / total_value) * 100, 2)
                if total_value
                else 0.0,
                "changePct1y": change_pct,
            }
        )
    set_indexes.sort(key=lambda x: x["totalValueUsd"], reverse=True)

    # --- Movers (gainers / losers across distinct cards) -----------------
    # Dedupe by card_id, keep the row with the largest |changePct1y|.
    by_card: dict[str, dict[str, Any]] = {}
    for h in holdings:
        if h["cardId"] is None or h["changePct1y"] is None:
            continue
        prev = by_card.get(h["cardId"])
        if prev is None or abs(h["changePct1y"]) > abs(prev["changePct1y"]):
            by_card[h["cardId"]] = h
    movers_pool = list(by_card.values())
    gainers = sorted(
        (m for m in movers_pool if m["changePct1y"] > 0),
        key=lambda m: m["changePct1y"],
        reverse=True,
    )[:3]
    losers = sorted(
        (m for m in movers_pool if m["changePct1y"] < 0),
        key=lambda m: m["changePct1y"],
    )[:3]

    def _mover_row(m: dict[str, Any]) -> dict[str, Any]:
        return {
            "gradeId": m["gradeId"],
            "cardId": m["cardId"],
            "cardName": m["cardName"],
            "cardImageUrl": m["cardImageUrl"],
            "setName": m["setName"],
            "valueUsd": round(m["valueUsd"], 2),
            "changePct1y": m["changePct1y"],
        }

    # --- Concentration (top-N share of total value) ----------------------
    sorted_by_value = sorted(holdings, key=lambda h: h["valueUsd"], reverse=True)
    if total_value > 0 and sorted_by_value:
        top1 = sorted_by_value[0]["valueUsd"]
        top3 = sum(h["valueUsd"] for h in sorted_by_value[:3])
        top5 = sum(h["valueUsd"] for h in sorted_by_value[:5])
        concentration = {
            "top1Pct": round((top1 / total_value) * 100, 2),
            "top3Pct": round((top3 / total_value) * 100, 2),
            "top5Pct": round((top5 / total_value) * 100, 2),
            "top1": {
                "cardName": sorted_by_value[0]["cardName"],
                "valueUsd": round(top1, 2),
            },
        }
    else:
        concentration = None

    # --- Year distribution (by decade) -----------------------------------
    decade_buckets: dict[int, dict[str, float]] = {}
    for h in holdings:
        if h["year"] is None:
            continue
        decade = (h["year"] // 10) * 10
        cur = decade_buckets.setdefault(decade, {"count": 0, "valueUsd": 0.0})
        cur["count"] += 1
        cur["valueUsd"] += h["valueUsd"]
    year_distribution = [
        {
            "decade": int(d),
            "count": int(v["count"]),
            "valueUsd": round(v["valueUsd"], 2),
        }
        for d, v in sorted(decade_buckets.items())
    ]

    # --- Grade distribution (UI bucket order) ----------------------------
    grade_distribution = [
        {"bucket": b, "count": grade_buckets[b]} for b in _GRADE_BUCKET_ORDER
    ]

    unique_sets = len({h["setName"] for h in holdings if h["setName"]})

    return {
        "stats": {
            "holdings": total_count,
            "uniqueSets": unique_sets,
            "avgGrade": round(avg_grade, 2),
            "gemRatePct": round(gem_rate_pct, 2),
            "avgValueUsd": round(total_value / total_count, 2) if total_count else 0.0,
            "oldestYear": year_min,
            "totalValueUsd": round(total_value, 2),
        },
        "kpis": {
            "totalScans": total_count,
            "avgGrade": round(avg_grade, 2),
            "gemRatePct": round(gem_rate_pct, 2),
            "lastScanAt": last_scan_at,
            "graderSplit": grader_split,
        },
        "setIndexes": set_indexes,
        "movers": {
            "gainers": [_mover_row(m) for m in gainers],
            "losers": [_mover_row(m) for m in losers],
        },
        "concentration": concentration,
        "yearDistribution": year_distribution,
        "gradeDistribution": grade_distribution,
    }


__all__ = ["build_overview"]
