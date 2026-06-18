"""PokemonPriceTracker provider — TCGplayer market price + eBay graded sales.

API docs: https://www.pokemonpricetracker.com/api-reference

This provider fills the gap left by eBay's approval-gated Browse /
Marketplace Insights APIs: it returns real eBay **sold** data for graded
slabs (PSA / CGC / BGS / SGC) alongside a TCGplayer-derived market price.
Competitor collection apps lean on aggregators like this for exactly the
same reason.

Capabilities
------------
* ``get_market_price`` — raw/ungraded market snapshot.
* ``search_sold_comps`` — one :class:`SoldComp` per graded tier reported by
  the upstream ``ebay`` block (e.g. PSA 8/9/10 averages).

Env-gated via ``POKEMONPRICETRACKER_API_KEY``; blank => not configured =>
omitted from every fan-out. All upstream errors are swallowed (returns
empty / ``None``) so the caller never sees a 5xx.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.config import get_settings
from app.integrations.base import BaseProvider, MarketPrice, SoldComp
from app.utils.logger import get_logger

logger = get_logger("integrations.pokemonpricetracker")

_BASE = "https://www.pokemonpricetracker.com/api/v2"

# Grade tiers the upstream ``ebay`` block reports, mapped to (house, grade).
# Extend here as the API surfaces more houses — the reducer ignores any
# tier it can't parse, so unknown keys degrade silently.
_GRADE_TIERS: dict[str, tuple[str, str]] = {
    "psa10": ("psa", "10"),
    "psa9": ("psa", "9"),
    "psa8": ("psa", "8"),
    "cgc10": ("cgc", "10"),
    "cgc9": ("cgc", "9"),
    "bgs10": ("bgs", "10"),
    "bgs9": ("bgs", "9"),
    "sgc10": ("sgc", "10"),
}


def _key() -> str | None:
    return getattr(get_settings(), "pokemonpricetracker_api_key", None) or None


class PokemonPriceTrackerProvider(BaseProvider):
    id = "pokemonpricetracker"
    name = "PokemonPriceTracker"

    def is_configured(self) -> bool:
        return _key() is not None

    async def get_market_price(self, query: str) -> MarketPrice | None:
        card = await self._fetch_card(query, include_ebay=False)
        if card is None:
            return None
        return self._reduce_price(card)

    async def search_sold_comps(
        self, query: str, *, days: int = 90, limit: int = 50
    ) -> list[SoldComp]:
        card = await self._fetch_card(query, include_ebay=True, days=days)
        if card is None:
            return []
        return self._reduce_comps(card)[:limit]

    # ---- HTTP -----------------------------------------------------------

    async def _fetch_card(
        self, query: str, *, include_ebay: bool, days: int = 90
    ) -> dict[str, Any] | None:
        if not self.is_configured() or not query:
            return None
        url = f"{_BASE}/cards?search={quote(query)}&limit=1"
        if include_ebay:
            url += f"&includeEbay=true&days={int(days)}"
        try:
            resp = await self._call_with_retry(
                "GET", url, headers={"Authorization": f"Bearer {_key()}"}
            )
            if resp is None or resp.status_code >= 400:
                return None
            data = resp.json() or {}
        except Exception as exc:
            logger.warning("pokemonpricetracker fetch failed: %s", exc)
            return None
        items = data.get("data") or data.get("cards") or []
        if isinstance(items, dict):
            items = [items]
        return items[0] if items else None

    # ---- Reducers -------------------------------------------------------

    @staticmethod
    def _reduce_price(card: dict[str, Any]) -> MarketPrice | None:
        prices = card.get("prices") or {}
        if not isinstance(prices, dict):
            return None
        market = _f(prices.get("market"))
        low = _f(prices.get("low"))
        high = _f(prices.get("high"))
        if not any((market, low, high)):
            return None
        return MarketPrice(
            source="pokemonpricetracker",
            market=market,
            low=low,
            high=high,
            extras={
                "card_name": card.get("name"),
                "card_id": card.get("id") or card.get("tcgPlayerId"),
            },
        )

    @staticmethod
    def _reduce_comps(card: dict[str, Any]) -> list[SoldComp]:
        ebay = card.get("ebay") or {}
        if not isinstance(ebay, dict):
            return []
        name = card.get("name") or "Unknown"
        sold_at = _now_iso()
        comps: list[SoldComp] = []
        for tier_key, (house, grade) in _GRADE_TIERS.items():
            tier = ebay.get(tier_key)
            if not isinstance(tier, dict):
                continue
            avg = _f(tier.get("avg") or tier.get("average"))
            if avg is None:
                continue
            comps.append(
                SoldComp(
                    source="pokemonpricetracker",
                    title=f"{name} {house.upper()} {grade}",
                    price=avg,
                    sold_at=sold_at,
                    grade=grade,
                    house=house,
                )
            )
        return comps


def _f(v: Any) -> float | None:
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["PokemonPriceTrackerProvider"]
