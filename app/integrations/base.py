"""Common base for real data providers.

* Module-level shared :class:`httpx.AsyncClient` (lazy, async-lock guarded).
* Lightweight value dataclasses for cross-provider normalization
  (``Listing`` / ``SoldComp`` / ``PopulationReport`` / ``MarketPrice``).
* :class:`BaseProvider` ABC with safe no-op defaults + retry helper.

Every public provider method MUST swallow upstream errors and return
empty/``None`` so callers never see 5xx.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.utils.logger import get_logger

logger = get_logger("integrations.base")

_USER_AGENT = "LoupeApp/1.0 (research)"
_TIMEOUT = httpx.Timeout(10.0)
_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def get_http_client() -> httpx.AsyncClient:
    """Return the process-wide :class:`httpx.AsyncClient` (lazy)."""
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            _client = httpx.AsyncClient(
                timeout=_TIMEOUT,
                limits=_LIMITS,
                headers={"User-Agent": _USER_AGENT},
            )
        return _client


async def close_http_client() -> None:
    """Close the shared client (call on app shutdown)."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception as exc:  # pragma: no cover
            logger.debug("http client close failed: %s", exc)
        _client = None


# ----------------------------------------------------------------- shapes


@dataclass
class Listing:
    """Live for-sale listing."""

    source: str
    title: str
    price: float
    currency: str = "USD"
    url: str = ""
    condition: str | None = None
    image_url: str | None = None
    is_auction: bool = False
    time_left_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SoldComp:
    """One completed sale."""

    source: str
    title: str
    price: float
    sold_at: str  # ISO 8601 UTC, ends with ``Z``
    currency: str = "USD"
    condition: str | None = None
    grade: str | None = None
    house: str | None = None
    url: str | None = None
    image_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PopulationReport:
    """Population for a single (house, grade) tier."""

    source: str
    house: str
    grade: str
    population: int
    pop_higher: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarketPrice:
    """Snapshot price for a product (low/mid/high/market in USD)."""

    source: str
    market: float | None = None
    low: float | None = None
    mid: float | None = None
    high: float | None = None
    currency: str = "USD"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------- helpers

_GRADE_RE = re.compile(
    r"\b(PSA|CGC|BGS|SGC|TAG)\s*(\d{1,2}(?:\.5)?)\b",
    re.IGNORECASE,
)


def parse_grade(title: str) -> tuple[str | None, str | None]:
    """Best-effort ``(house, grade)`` extraction from listing/comp titles."""
    if not title:
        return None, None
    m = _GRADE_RE.search(title)
    if not m:
        return None, None
    return m.group(1).lower(), m.group(2)


# ----------------------------------------------------------------- base


class BaseProvider(ABC):
    """Abstract base — overrides decide which capabilities a provider supports."""

    #: Stable lower-case id (eg. ``"ebay"``).
    id: str = ""
    #: Human label (eg. ``"eBay"``).
    name: str = ""

    @abstractmethod
    def is_configured(self) -> bool:
        """``True`` when the provider has the env credentials it needs."""

    # ---- Capability defaults — override only what you implement. ---------

    async def search_listings(self, query: str, *, limit: int = 20) -> list[Listing]:
        return []

    async def search_sold_comps(
        self, query: str, *, days: int = 90, limit: int = 50
    ) -> list[SoldComp]:
        return []

    async def get_population(self, spec_or_query: str) -> list[PopulationReport] | None:
        return None

    async def get_market_price(self, query: str) -> MarketPrice | None:
        return None

    # ---- Retry helper ----------------------------------------------------

    async def _call_with_retry(
        self,
        method: str,
        url: str,
        *,
        retries: int = 2,
        backoff: tuple[float, ...] = (0.5, 1.5),
        **kwargs: Any,
    ) -> httpx.Response | None:
        """Issue *method* *url* with simple exponential backoff.

        Catches **all** exceptions and returns ``None`` on terminal failure.
        """
        client = await get_http_client()
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await client.request(method, url, **kwargs)
                if resp.status_code >= 500 and attempt < retries:
                    raise httpx.HTTPStatusError(
                        f"{resp.status_code}", request=resp.request, response=resp
                    )
                return resp
            except Exception as exc:
                last_exc = exc
                if attempt >= retries:
                    break
                delay = backoff[min(attempt, len(backoff) - 1)]
                await asyncio.sleep(delay)
        logger.warning("provider call failed (%s %s): %s", method, url, last_exc)
        return None


__all__ = [
    "BaseProvider",
    "Listing",
    "MarketPrice",
    "PopulationReport",
    "SoldComp",
    "close_http_client",
    "get_http_client",
    "parse_grade",
]
