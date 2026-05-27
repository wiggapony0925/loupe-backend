"""Rich per-card market snapshot synthesizer.

Until we wire real graded-comp providers (PSA pop reports, eBay sold
comps, 130point, PWCC index), we synthesize a deterministic per-house ×
per-grade table seeded by the card id so the detail page is steady
across refreshes.

The base "raw" price is the live ungraded market from
``card.pricing_summary.market`` (TCGplayer / Cardmarket / Scryfall),
so when real prices move, every graded tier moves with them.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from app.platform.cache_config import PRICE_HISTORY_TTL
from app.platform.redis_client import get_redis
from app.integrations import get_registry
from app.services.catalog import card_search_service
from app.utils.logger import get_logger

# Real-data enrichment runs only for top tiers per house (10/9.5/9).
_REAL_DATA_TOP_GRADES: tuple[float, ...] = (10, 9.5, 9)

logger = get_logger("services.market")

# House drift vs PSA baseline (1.0) — keeps cross-house comparisons sane.
_HOUSE_DRIFT: dict[str, float] = {
    "psa": 1.00,
    "cgc": 0.95,
    "bgs": 1.05,
    "sgc": 0.92,
    "tag": 0.85,
}
_HOUSES: tuple[str, ...] = ("psa", "cgc", "bgs", "sgc", "tag")
_GRADES: tuple[float, ...] = (10, 9.5, 9, 8.5, 8, 7, 6, 5, 4, 3)
_RANGES: tuple[str, ...] = ("30d", "90d", "1y", "all")

# Stable reference timestamp for synthesized last-sale dates (deterministic).
_LAST_SALE_EPOCH = datetime(2025, 1, 1, tzinfo=UTC)

# Grade multipliers vs raw — (low, high). Vintage gets pushed toward high
# end, modern toward low end, via _vintage_factor() below.
_GRADE_MULT: dict[float, tuple[float, float]] = {
    10: (10.0, 18.0),
    9.5: (5.0, 8.0),
    9: (2.5, 4.0),
    8.5: (1.6, 2.2),
    8: (1.2, 1.6),
    7: (0.9, 1.1),
    6: (0.70, 0.78),
    5: (0.55, 0.62),
    4: (0.45, 0.52),
    3: (0.35, 0.40),
}


def _seeded_rng(card_id: str, *salt: str) -> random.Random:
    key = "|".join((card_id, *salt))
    seed = int(hashlib.md5(key.encode()).hexdigest(), 16) % (2**32)
    return random.Random(seed)


def _vintage_factor(year: int | None) -> float:
    """Return 0.0 (full modern) … 1.0 (full vintage) for grade-mult lerp."""
    if not year:
        return 0.4
    if year <= 1995:
        return 1.0
    if year >= 2020:
        return 0.0
    # Linear ramp 1995 → 2020.
    return max(0.0, min(1.0, (2020 - year) / 25.0))


def _grade_label(house: str, grade: float) -> str:
    if grade == 10 and house == "bgs":
        return "10"
    if grade == int(grade):
        return str(int(grade))
    return f"{grade:.1f}"


def _money(amount: float) -> dict[str, Any]:
    return {"amount": round(max(0.01, amount), 2), "currency": "USD"}


def _population(
    rng: random.Random,
    grade: float,
    year: int | None,
) -> int:
    """Pareto-ish: top grades scarce on vintage, common on modern."""
    is_modern = (year or 2000) >= 2010
    base = rng.randint(50, 500) if is_modern else rng.randint(1, 30)
    decay = 1.6 ** (10 - grade)
    pop = int(base * decay)
    # Cap so we don't overflow display fields.
    return max(1, min(pop, 50_000))


def _house_grade_row(
    card_id: str,
    house: str,
    grade: float,
    raw_amt: float,
    year: int | None,
) -> dict[str, Any]:
    rng = _seeded_rng(card_id, house, str(grade))
    lo, hi = _GRADE_MULT[grade]
    vf = _vintage_factor(year)
    # Vintage → push toward high end of the multiplier band.
    mult = lo + (hi - lo) * (0.25 + 0.75 * vf) * (0.85 + 0.30 * rng.random())
    drift = _HOUSE_DRIFT.get(house, 1.0)
    price = raw_amt * mult * drift
    change_pct = round(-30 + rng.random() * 80, 2)  # ±-30..+50%
    pop = _population(rng, grade, year)
    last_sale_days = rng.randint(0, 60)
    # Anchor to a fixed epoch so the row is byte-identical across calls.
    last_sale = _LAST_SALE_EPOCH - timedelta(days=last_sale_days)
    return {
        "house": house,
        "grade": grade,
        "grade_label": _grade_label(house, grade),
        "population": pop,
        "market": _money(price),
        "change_pct": change_pct,
        "last_sale_at": last_sale.isoformat(),
        "listing_url": None,
        "source": "synthesized",
    }


def _build_houses(
    card_id: str,
    raw_amt: float,
    year: int | None,
) -> list[dict[str, Any]]:
    is_modern = (year or 2000) >= 2020
    blocks: list[dict[str, Any]] = []
    for house in _HOUSES:
        rows: list[dict[str, Any]] = []
        for grade in _GRADES:
            # Modern cards skip the lowest-grade noise.
            if is_modern and grade <= 4:
                continue
            rows.append(_house_grade_row(card_id, house, grade, raw_amt, year))
        # BGS polish: emit a "10 BLACK" row above the regular 10.
        if house == "bgs":
            rng = _seeded_rng(card_id, "bgs", "black")
            black = dict(rows[0])
            black["grade_label"] = "10 BLACK"
            # Black labels carry a premium over BGS 10.
            base_amt = float(black["market"]["amount"])
            black["market"] = _money(base_amt * (1.4 + rng.random() * 0.6))
            black["population"] = max(1, int(black["population"] * 0.15))
            rows.insert(0, black)
        pop_total = sum(int(r["population"]) for r in rows)
        blocks.append({"house": house, "pop_total": pop_total, "grades": rows})
    blocks.sort(key=lambda b: int(b["pop_total"]), reverse=True)
    return blocks


def _summary(
    houses: list[dict[str, Any]],
    raw_amt: float | None,
    change_pct_1y: float,
) -> dict[str, Any]:
    if raw_amt is None or not houses:
        return {
            "raw": None,
            "graded_avg": None,
            "pop_top": None,
            "pop_total": 0,
            "change_pct_1y": 0.0,
            "last_sale_at": None,
            "primary_house": "psa",
        }
    pop_total = sum(int(b["pop_total"]) for b in houses)
    weighted_sum = 0.0
    for b in houses:
        for r in b["grades"]:
            weighted_sum += float(r["market"]["amount"]) * int(r["population"])
    graded_avg = weighted_sum / pop_total if pop_total else 0.0
    # Top tier — pull PSA 10 if available, else first house's first row.
    pop_top_money: dict[str, Any] | None = None
    last_sale: str | None = None
    for b in houses:
        if b["house"] == "psa":
            for r in b["grades"]:
                if r["grade"] == 10:
                    pop_top_money = r["market"]
                    last_sale = r.get("last_sale_at")
                    break
        if pop_top_money is not None:
            break
    if pop_top_money is None and houses:
        first = houses[0]["grades"][0]
        pop_top_money = first["market"]
        last_sale = first.get("last_sale_at")
    return {
        "raw": _money(raw_amt),
        "graded_avg": _money(graded_avg),
        "pop_top": pop_top_money,
        "pop_total": pop_total,
        "change_pct_1y": round(change_pct_1y, 2),
        "last_sale_at": last_sale,
        "primary_house": "psa",
    }


async def _build_history(card_id: str) -> dict[str, Any]:
    history: dict[str, Any] = {}
    for r in _RANGES:
        body = await card_search_service.get_price_history(card_id, range_=r)
        if body is None:
            continue
        # Keep only the price-history shape; strip echoed house/grade.
        body.pop("house", None)
        body.pop("grade", None)
        history[r] = body
    return history


async def build_market_for_card(card: dict[str, Any]) -> dict[str, Any]:
    """Compose a :class:`MarketSnapshot` payload for one card dict."""
    card_id = card["id"]
    pricing = card.get("pricing_summary") or {}
    market_obj = pricing.get("market") if pricing else None
    raw_amt = (
        float(market_obj["amount"])
        if isinstance(market_obj, dict) and market_obj.get("amount")
        else None
    )
    year = card.get("year")

    history = await _build_history(card_id)
    one_year = history.get("1y") or {}
    change_pct_1y = (one_year.get("summary") or {}).get("change_pct") or 0.0

    if raw_amt is None:
        snapshot = {
            "summary": _summary([], None, 0.0),
            "history": history,
            "houses": [],
            "tiers_total": 0,
        }
        return snapshot

    houses = _build_houses(card_id, raw_amt, year)
    try:
        await _enrich_with_real_data(card, houses)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("market real-data enrichment failed: %s", exc)
    tiers_total = sum(len(b["grades"]) for b in houses)
    summary = _summary(houses, raw_amt, float(change_pct_1y))
    return {
        "summary": summary,
        "history": history,
        "houses": houses,
        "tiers_total": tiers_total,
    }


# ----------------------------------------------------------------- real data


def _build_query(card: dict[str, Any]) -> str:
    bits: list[str] = []
    for key in ("name", "set_name", "number"):
        v = card.get(key)
        if v:
            bits.append(str(v))
    return " ".join(bits).strip()


async def _enrich_with_real_data(
    card: dict[str, Any], houses: list[dict[str, Any]]
) -> None:
    """Promote synthesized top-grade rows to ``source="real"`` when comps exist.

    Only touches grades in :data:`_REAL_DATA_TOP_GRADES`. Average comp price
    per ``(house, grade)`` overrides the synthesized market value.
    """
    query = _build_query(card)
    if not query:
        return
    registry = get_registry()
    try:
        comps = await registry.fan_out_comps(query, days=90, limit=100)
    except Exception as exc:
        from app.platform.observability import capture_integration_error

        capture_integration_error(
            exc,
            integration="market.registry",
            operation="fan_out_comps",
            extra={"query": query, "card_id": card.get("id")},
        )
        return
    if not comps:
        return

    buckets: dict[tuple[str, str], list[Any]] = {}
    for c in comps:
        if not c.house or not c.grade:
            continue
        buckets.setdefault((c.house.lower(), str(c.grade)), []).append(c)

    for block in houses:
        for row in block["grades"]:
            try:
                grade = float(row["grade"])
            except (TypeError, ValueError):
                continue
            if grade not in _REAL_DATA_TOP_GRADES:
                continue
            key = (
                str(row["house"]).lower(),
                str(int(grade) if grade == int(grade) else grade),
            )
            entries = buckets.get(key)
            if not entries:
                continue
            try:
                avg = sum(float(e.price) for e in entries) / len(entries)
                latest = max(entries, key=lambda e: e.sold_at)
                row["market"] = _money(avg)
                row["last_sale_at"] = latest.sold_at
                row["listing_url"] = getattr(latest, "url", None)
                row["source"] = "real"
            except Exception as exc:
                logger.debug("row enrichment failed: %s", exc)


# ----------------------------------------------------------------- caching


_CACHE_PREFIX = "loupe:cards:market"


async def _cache_get(key: str) -> dict[str, Any] | None:
    try:
        r = await get_redis()
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:  # pragma: no cover
        logger.debug("market cache_get failed: %s", exc)
        return None


async def _cache_set(key: str, value: dict[str, Any]) -> None:
    try:
        r = await get_redis()
        await r.setex(key, PRICE_HISTORY_TTL, json.dumps(value, default=str))
    except Exception as exc:  # pragma: no cover
        logger.debug("market cache_set failed: %s", exc)


async def get_card_market(card_id: str) -> dict[str, Any] | None:
    """Resolve a card and return its full market snapshot envelope body."""
    cache_key = f"{_CACHE_PREFIX}:{card_id}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    card = await card_search_service.get_card(card_id)
    if card is None:
        return None
    snapshot = await build_market_for_card(card)
    body = {"card_id": card_id, "snapshot": snapshot}
    await _cache_set(cache_key, body)
    return body


__all__ = ["build_market_for_card", "get_card_market"]
