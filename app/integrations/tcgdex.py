"""TCGdex provider — keyless Pokémon market prices by exact card id.

TCGdex includes Cardmarket / TCGplayer pricing directly on card responses.
Unlike fuzzy marketplace search, the direct card endpoint lets us fetch
``pokemontcg:base1-8`` as ``base1-8`` and avoid returning prices for the
wrong print when eBay is unavailable.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.integrations.base import BaseProvider, MarketPrice
from app.utils.logger import get_logger

logger = get_logger("integrations.tcgdex")

_BASE = "https://api.tcgdex.net/v2/en"


class TcgDexProvider(BaseProvider):
    id = "tcgdex"
    name = "TCGdex"

    def is_configured(self) -> bool:
        return True

    async def get_market_price(self, query: str) -> MarketPrice | None:
        """Capability shim used by generic fan-out.

        Generic market-price fan-out passes a search query, but TCGdex is
        safest by exact Pokémon card id. If callers pass an id-like string,
        return the first marketplace quote; otherwise opt out.
        """
        quotes = await self.get_market_prices_for_card_id(query)
        return quotes[0] if quotes else None

    async def get_market_prices_for_card_id(self, card_id: str) -> list[MarketPrice]:
        upstream_id = _normalize_pokemon_card_id(card_id)
        if not upstream_id:
            return []

        url = f"{_BASE}/cards/{quote(upstream_id, safe='-')}"
        try:
            resp = await self._call_with_retry("GET", url)
            if resp is None or resp.status_code >= 400:
                return []
            payload = resp.json() or {}
        except Exception as exc:
            logger.debug("tcgdex card price fetch failed: %s", exc)
            return []

        return _reduce_card(payload)


def _normalize_pokemon_card_id(card_id: str) -> str | None:
    value = (card_id or "").strip()
    if not value:
        return None
    if ":" in value:
        source, _, upstream = value.partition(":")
        if source.lower() not in {"pokemontcg", "tcgdex"}:
            return None
        value = upstream
    if "-" not in value:
        return None
    return value


def _reduce_card(card: dict[str, Any]) -> list[MarketPrice]:
    pricing = card.get("pricing") or {}
    if not isinstance(pricing, dict):
        return []

    out: list[MarketPrice] = []
    card_meta = {
        "card_id": card.get("id"),
        "card_name": card.get("name"),
        "set_name": (card.get("set") or {}).get("name"),
        "provider": "tcgdex",
    }

    cardmarket = pricing.get("cardmarket")
    if isinstance(cardmarket, dict):
        market = _first_number(
            cardmarket,
            "trend-holo",
            "trend",
            "avg30-holo",
            "avg30",
            "avg-holo",
            "avg",
        )
        low = _first_number(cardmarket, "low-holo", "low")
        mid = _first_number(cardmarket, "avg-holo", "avg", "avg7-holo", "avg7")
        if any(v is not None for v in (market, low, mid)):
            out.append(
                MarketPrice(
                    source="cardmarket",
                    market=market,
                    low=low,
                    mid=mid,
                    high=None,
                    currency=str(cardmarket.get("unit") or "EUR"),
                    extras={
                        **card_meta,
                        "as_of": cardmarket.get("updated"),
                        "price_kind": "trend",
                        "source_provider": "tcgdex",
                    },
                )
            )

    tcgplayer = pricing.get("tcgplayer")
    if isinstance(tcgplayer, dict):
        quote = _reduce_tcgplayer(tcgplayer, card_meta)
        if quote is not None:
            out.append(quote)

    return out


def _reduce_tcgplayer(
    tcgplayer: dict[str, Any], card_meta: dict[str, Any]
) -> MarketPrice | None:
    best_variant: tuple[str, dict[str, Any]] | None = None
    for key in ("holo", "normal", "reverse", "firstEditionHolo", "firstEditionNormal"):
        value = tcgplayer.get(key)
        if isinstance(value, dict) and any(value.values()):
            best_variant = (key, value)
            break
    if best_variant is None:
        for key, value in tcgplayer.items():
            if isinstance(value, dict) and any(value.values()):
                best_variant = (str(key), value)
                break
    if best_variant is None:
        return None

    variant_name, prices = best_variant
    market = _first_number(prices, "marketPrice", "market", "market_price")
    low = _first_number(prices, "lowPrice", "low", "low_price")
    mid = _first_number(prices, "midPrice", "mid", "mid_price")
    high = _first_number(prices, "highPrice", "high", "high_price")
    if not any(v is not None for v in (market, low, mid, high)):
        return None
    return MarketPrice(
        source="tcgplayer",
        market=market,
        low=low,
        mid=mid,
        high=high,
        currency=str(tcgplayer.get("unit") or "USD"),
        extras={
            **card_meta,
            "as_of": tcgplayer.get("updated"),
            "variant": variant_name,
            "source_provider": "tcgdex",
        },
    )


def _first_number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = _f(data.get(key))
        if parsed is not None:
            return parsed
    return None


def _f(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        if parsed <= 0:
            return None
        return round(parsed, 2)
    except (TypeError, ValueError):
        return None


__all__ = ["TcgDexProvider"]
