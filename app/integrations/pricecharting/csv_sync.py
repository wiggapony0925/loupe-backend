"""Legendary bulk price-guide mirror — download the daily CSV once, serve
per-card prices from Postgres forever after (instant, unlimited, quota-free).

This is the *pre-built* Legendary path. It is inert on every lower tier: the
mirror table ships empty, :func:`lookup_market_price` short-circuits to ``None``
when empty (so the API path runs), and :func:`sync_price_guide` no-ops without a
CSV URL. The second a Legendary CSV is synced, per-card lookups start resolving
from the mirror automatically — no code change, no flag flip.

CSV columns match the Prices API key names, so the exact same
:mod:`.grades` helpers turn a CSV row into the same ladder the API path builds.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import delete, func, select

from app.config import get_settings
from app.integrations.base import MarketPrice
from app.integrations.pricecharting import grades
from app.models.pricecharting_price import PriceChartingPrice
from app.utils.logger import get_logger

logger = get_logger("integrations.pricecharting.csv_sync")

_DOWNLOAD_TIMEOUT = 120.0
_UPSERT_CHUNK = 1000

# Readiness is cached so the hot per-card path never touches the DB when the
# mirror is empty (i.e. on every non-Legendary tier).
_ready_cache: bool | None = None


def _sessionmaker():  # type: ignore[no-untyped-def]
    from app.db import get_sessionmaker

    return get_sessionmaker()


def reset_ready_cache() -> None:
    global _ready_cache
    _ready_cache = None


def _tcg_from_console(console: str | None) -> str | None:
    c = (console or "").lower()
    if "pokemon" in c:
        return "pokemon"
    if "magic" in c:
        return "magic"
    if "yugioh" in c or "yu-gi-oh" in c:
        return "yugioh"
    return None


def _norm(s: str | None) -> str:
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in (s or "")).split()
    ).lower()


def _row_to_model(raw: dict[str, Any]) -> PriceChartingPrice | None:
    """A CSV/API product dict → a mirror row (or None if it carries no price)."""
    pc_id = raw.get("id")
    name = raw.get("product-name")
    if not pc_id or not name:
        return None
    ladder = grades.card_grade_ladder(raw)
    loose = grades.cents_to_dollars(raw.get("loose-price"))
    if not ladder and loose is None:
        return None
    console = raw.get("console-name")
    return PriceChartingPrice(
        id=str(pc_id),
        product_name=str(name)[:300],
        product_name_lower=_norm(name)[:300],
        console_name=(str(console)[:200] if console else None),
        console_lower=(_norm(console)[:200] if console else None),
        tcg=_tcg_from_console(console),
        loose_price=loose,
        ladder=ladder or None,
        sales_volume=grades.int_or_none(raw.get("sales-volume")),
        release_date=(
            str(raw.get("release-date")) if raw.get("release-date") else None
        ),
        synced_at=datetime.now(UTC),
    )


# ── status ────────────────────────────────────────────────────────────────


async def get_status() -> dict[str, Any]:
    """Row count + last sync time for the dev-portal page."""
    try:
        maker = _sessionmaker()
        async with maker() as session:
            rows = int(
                (
                    await session.execute(select(func.count(PriceChartingPrice.id)))
                ).scalar()
                or 0
            )
            synced = (
                await session.execute(select(func.max(PriceChartingPrice.synced_at)))
            ).scalar()
    except Exception as exc:  # pragma: no cover - portal must never 500 on this
        logger.debug("pricecharting mirror status failed: %s", exc)
        return {"ready": False, "rows": 0, "synced_at": None}
    return {
        "ready": rows > 0,
        "rows": rows,
        "synced_at": synced.isoformat() if synced else None,
    }


async def is_ready() -> bool:
    global _ready_cache
    if _ready_cache is None:
        _ready_cache = (await get_status())["ready"]
    return _ready_cache


# ── sync ──────────────────────────────────────────────────────────────────


async def _download_csv(url: str) -> str:
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        return resp.text


def _parse_csv(text: str) -> list[dict[str, Any]]:
    return list(csv.DictReader(io.StringIO(text)))


async def sync_price_guide(
    *, url: str | None = None, rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Full-refresh the local mirror from the Legendary CSV.

    Pass ``rows`` to sync already-parsed data (tests / re-use); otherwise the
    CSV is downloaded from ``url`` (defaults to ``PRICECHARTING_CSV_URL``).
    Returns ``{ok, rows, reason?}``. A missing URL is a no-op, not an error —
    that's just "not Legendary yet".
    """
    if rows is None:
        url = url or get_settings().pricecharting_csv_url
        if not url:
            return {"ok": False, "rows": 0, "reason": "no_csv_url"}
        try:
            rows = _parse_csv(await _download_csv(url))
        except Exception as exc:
            logger.warning("pricecharting CSV download failed: %s", exc)
            return {"ok": False, "rows": 0, "reason": f"download_failed: {exc}"}

    models = [m for raw in rows if (m := _row_to_model(raw)) is not None]
    maker = _sessionmaker()
    async with maker() as session:
        # Full snapshot: replace the guide atomically so stale rows can't linger.
        await session.execute(delete(PriceChartingPrice))
        for i in range(0, len(models), _UPSERT_CHUNK):
            session.add_all(models[i : i + _UPSERT_CHUNK])
            await session.flush()
        await session.commit()
    reset_ready_cache()
    logger.info("pricecharting mirror synced: %d rows", len(models))
    return {"ok": True, "rows": len(models)}


# ── mirror-first lookup ─────────────────────────────────────────────────────


def _row_market_price(row: PriceChartingPrice) -> MarketPrice:
    ladder = dict(row.ladder or {})
    loose = row.loose_price if row.loose_price is not None else ladder.get("UNGRADED")
    new = ladder.get("PSA 8")
    graded = ladder.get("PSA 9") or ladder.get("PSA 10")
    extras: dict[str, Any] = {
        "product_name": row.product_name,
        "console": row.console_name,
        "pc_id": row.id,
        "source_detail": "mirror",
    }
    if ladder:
        extras["grade_ladder"] = ladder
    if row.sales_volume is not None:
        extras["sales_volume"] = row.sales_volume
    if row.release_date:
        extras["release_date"] = row.release_date
    return MarketPrice(
        source="pricecharting",
        market=graded or loose or new,
        low=loose,
        mid=new,
        high=graded,
        extras=extras,
    )


async def lookup_market_price(query: str) -> MarketPrice | None:
    """Serve a card price from the local mirror — or ``None`` to fall back to
    the API. Conservative on purpose: only an **exact** normalised product-name
    match wins, so we never return the wrong printing's price. (Fuzzier matching
    can be layered on once we've tuned it against a real synced guide.)"""
    if not query or not await is_ready():
        return None
    norm = _norm(query)
    if not norm:
        return None
    try:
        maker = _sessionmaker()
        async with maker() as session:
            row = (
                await session.execute(
                    select(PriceChartingPrice)
                    .where(PriceChartingPrice.product_name_lower == norm)
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception as exc:  # pragma: no cover - never break a price read
        logger.debug("pricecharting mirror lookup failed: %s", exc)
        return None
    return _row_market_price(row) if row is not None else None


__all__ = [
    "get_status",
    "is_ready",
    "lookup_market_price",
    "reset_ready_cache",
    "sync_price_guide",
]
