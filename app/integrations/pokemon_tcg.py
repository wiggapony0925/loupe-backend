"""Pokémon TCG API provider — extracts inline TCGplayer + Cardmarket prices.

The free https://pokemontcg.io API returns ``tcgplayer.prices`` and
``cardmarket.prices`` blocks on every card. This provider piggy-backs on
those — no separate TCGplayer/Cardmarket API key required.

Only the *market price* capability is implemented (no listings/comps).
Configured iff we can reach the API; the optional ``POKEMON_TCG_API_KEY``
just raises rate limits.
"""

from __future__ import annotations

from typing import Any

from app.integrations._http import pokemon_tcg
from app.integrations.base import BaseProvider, MarketPrice
from app.utils.logger import get_logger

logger = get_logger("integrations.pokemon_tcg")


class PokemonTcgProvider(BaseProvider):
    id = "pokemon_tcg"
    name = "Pokémon TCG API"

    def is_configured(self) -> bool:
        # Public endpoint — always available. The key is optional.
        return True

    async def get_market_price(self, query: str) -> MarketPrice | None:
        if not query:
            return None
        try:
            payload = await pokemon_tcg.search_cards(query, page=1, page_size=1)
        except Exception as exc:
            logger.debug("pokemon_tcg search failed: %s", exc)
            return None
        cards = (payload or {}).get("data") or []
        if not cards:
            return None
        return self._reduce(cards[0])

    @staticmethod
    def _reduce(card: dict[str, Any]) -> MarketPrice | None:
        tcg = (card.get("tcgplayer") or {}).get("prices") or {}
        cm = (card.get("cardmarket") or {}).get("prices") or {}

        # tcgplayer.prices has variant keys: normal, holofoil, reverseHolofoil,
        # 1stEditionHolofoil, etc. Pick the first variant with data.
        variant: dict[str, Any] | None = None
        for key in (
            "holofoil",
            "normal",
            "reverseHolofoil",
            "1stEditionHolofoil",
            "1stEditionNormal",
            "unlimitedHolofoil",
        ):
            v = tcg.get(key)
            if isinstance(v, dict) and any(v.values()):
                variant = v
                break
        if variant is None and tcg:
            for v in tcg.values():
                if isinstance(v, dict) and any(v.values()):
                    variant = v
                    break

        market: float | None = None
        low: float | None = None
        mid: float | None = None
        high: float | None = None

        if variant:
            market = _f(variant.get("market"))
            low = _f(variant.get("low"))
            mid = _f(variant.get("mid"))
            high = _f(variant.get("high"))

        # Fall back to Cardmarket EUR averages when TCGplayer is empty.
        if market is None and cm:
            market = _f(cm.get("averageSellPrice") or cm.get("trendPrice"))
            low = low or _f(cm.get("lowPrice"))

        if not any((market, low, mid, high)):
            return None

        return MarketPrice(
            source="pokemon_tcg",
            market=market,
            low=low,
            mid=mid,
            high=high,
            extras={
                "card_id": card.get("id"),
                "card_name": card.get("name"),
                "set_id": (card.get("set") or {}).get("id"),
                "tcgplayer_url": (card.get("tcgplayer") or {}).get("url"),
                "cardmarket_url": (card.get("cardmarket") or {}).get("url"),
            },
        )


def _f(v: Any) -> float | None:
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["PokemonTcgProvider"]
