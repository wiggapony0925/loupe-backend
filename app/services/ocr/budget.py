"""Monthly OCR spend tracker with a tiny in-process cache.

Why this exists
---------------
Google Cloud Vision is metered. A runaway client (or a hot retry loop)
could rack up a four-figure bill before anyone noticed. Rather than
trust manual GCP budget alerts (lagging, project-wide, no app context),
we enforce a soft cap *inside* the identification pipeline:

    1. Every successful Vision call writes ``CardIdentification.cost_usd``.
    2. Before each new call, :func:`is_budget_exceeded` sums the
       current calendar month's ``cost_usd`` and compares it to
       ``OCR_MONTHLY_BUDGET_USD``.
    3. When the budget is exhausted the pipeline does **not** call
       Vision. Instead :class:`CardIdentifier` short-circuits with
       ``ocr_provider="client_fallback"`` and ``fallback_required=True``
       so the client can run on-device OCR (Apple Vision / ML Kit) and
       resubmit the parsed text via ``POST /v1/cards/identify/text``.

The cache (60s TTL) keeps the budget check from adding a DB round-trip
to every identify call once the cap is close.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.identification import CardIdentification

_CACHE_TTL_SECONDS = 60.0


@dataclass
class _CachedSpend:
    month_key: str  # "YYYY-MM"
    spend_usd: float
    fetched_at: float  # monotonic seconds


_cache: _CachedSpend | None = None


def _month_key(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}"


def _month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def get_month_to_date_spend_usd(db: AsyncSession) -> float:
    """Sum ``cost_usd`` for all identifications in the current UTC month.

    Cached for :data:`_CACHE_TTL_SECONDS` seconds per-process. The cache
    is keyed by month, so the rollover at month boundaries is automatic.
    """
    global _cache
    now_mono = time.monotonic()
    key = _month_key()
    if (
        _cache is not None
        and _cache.month_key == key
        and (now_mono - _cache.fetched_at) < _CACHE_TTL_SECONDS
    ):
        return _cache.spend_usd

    total = (
        await db.execute(
            select(func.coalesce(func.sum(CardIdentification.cost_usd), 0.0)).where(
                CardIdentification.created_at >= _month_start()
            )
        )
    ).scalar_one()
    spend = float(total or 0.0)
    _cache = _CachedSpend(month_key=key, spend_usd=spend, fetched_at=now_mono)
    return spend


async def is_budget_exceeded(db: AsyncSession) -> bool:
    """``True`` when month-to-date spend ≥ ``OCR_MONTHLY_BUDGET_USD``.

    Budget ≤ 0 disables the cap (useful for dev / staging).
    """
    settings = get_settings()
    budget = settings.ocr_monthly_budget_usd
    if budget <= 0:
        return False
    return await get_month_to_date_spend_usd(db) >= budget


def record_spend_increment(amount_usd: float) -> None:
    """Bump the cached running total without re-querying the DB.

    Called from :class:`CardIdentifier` right after a successful Vision
    call so the next request sees the new total instantly even if the
    60s cache window has not expired.
    """
    global _cache
    if amount_usd <= 0:
        return
    key = _month_key()
    if _cache is None or _cache.month_key != key:
        _cache = _CachedSpend(
            month_key=key, spend_usd=amount_usd, fetched_at=time.monotonic()
        )
    else:
        _cache.spend_usd += amount_usd


def reset_cache_for_tests() -> None:
    """Test hook — never call from production code."""
    global _cache
    _cache = None


__all__ = [
    "get_month_to_date_spend_usd",
    "is_budget_exceeded",
    "record_spend_increment",
    "reset_cache_for_tests",
]
