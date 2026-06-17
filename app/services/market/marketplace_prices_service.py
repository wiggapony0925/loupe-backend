"""Per-marketplace real market rows for a card.

This endpoint intentionally separates two different concepts:

* ``kind="listing"`` — a provider returned an actual active seller row.
* ``kind="market_price"`` — a provider returned current market pricing,
  but not a seller-specific active listing.

That distinction lets the card-detail UI stay useful while eBay is
unavailable without pretending price-guide data is live inventory.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote_plus

from app.domain.market import Listing, MarketPrice
from app.integrations import get_registry
from app.integrations.tcgdex import TcgDexProvider
from app.platform.redis_client import get_redis
from app.services.catalog import card_search_service
from app.services.market.listings_service import _build_query
from app.utils.logger import get_logger
from app.utils.time import utcnow

logger = get_logger("services.marketplace_prices")

_CACHE_PREFIX = "loupe:cards:marketplace-prices"
_CACHE_TTL = 180

# Provider → "search URL" template. ``{q}`` is the URL-encoded query.
# Search actions are always returned so the app can show useful exits
# without relying on eBay.
_SEARCH_URL_TEMPLATE: dict[str, str] = {
    "ebay": "https://www.ebay.com/sch/i.html?_nkw={q}&LH_BIN=1",
    "tcgplayer": "https://www.tcgplayer.com/search/all/product?q={q}",
    "cardmarket": "https://www.cardmarket.com/en/Pokemon/Products/Search?searchString={q}",
    "pricecharting": "https://www.pricecharting.com/search-products?q={q}&type=prices",
    "google_shopping": "https://www.google.com/search?tbm=shop&q={q}",
}


def _provider_label(source: str) -> str:
    s = source.lower().strip()
    return {
        "cardmarket": "Cardmarket",
        "ebay": "eBay",
        "tcgplayer": "TCGplayer",
        "130point": "130point",
        "psa": "PSA",
        "cgc": "CGC",
        "pricecharting": "PriceCharting",
        "pokemontcg": "Pokémon TCG API",
        "pokemon_tcg": "Pokémon TCG API",
        "tcgdex": "TCGdex",
        "justtcg": "JustTCG",
        "google_shopping": "Google Shopping",
    }.get(s, source)


async def _cache_get(key: str) -> Any | None:
    try:
        r = await get_redis()
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:  # pragma: no cover
        logger.debug("marketplace_prices cache_get failed: %s", exc)
        return None


async def _cache_set(key: str, value: Any) -> None:
    try:
        r = await get_redis()
        await r.setex(key, _CACHE_TTL, json.dumps(value, default=str))
    except Exception as exc:  # pragma: no cover
        logger.debug("marketplace_prices cache_set failed: %s", exc)


async def get_marketplace_prices_for_card(
    card_id: str, *, limit: int = 50
) -> dict[str, Any] | None:
    """Return live listing summaries + real market price rows."""
    cache_key = f"{_CACHE_PREFIX}:{card_id}:{limit}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    card = await card_search_service.get_card(card_id)
    if card is None:
        return None

    query = _build_query(card)
    registry = get_registry()
    listing_task = registry.fan_out_listings(query, limit=limit)
    market_task = registry.fan_out_market_price(query)
    tcgdex_task = TcgDexProvider().get_market_prices_for_card_id(card_id)

    raw_listings, fanout_quotes, tcgdex_quotes = await asyncio.gather(
        _safe_listings(listing_task),
        _safe_quotes(market_task),
        _safe_quotes(tcgdex_task),
    )

    encoded = quote_plus(query) if query else ""
    actions = _search_actions(encoded)

    catalog_quote = _catalog_pricing_quote(card)
    quotes = [
        q for q in (catalog_quote, *fanout_quotes, *tcgdex_quotes) if q is not None
    ]

    rows = _listing_rows(raw_listings, encoded)
    rows.extend(_market_price_rows(_dedupe_quotes(quotes), encoded))

    rows.sort(key=_sort_key)
    body = {
        "card_id": card_id,
        "query": query,
        "generated_at": utcnow().isoformat(),
        "providers": rows,
        "actions": actions,
    }
    await _cache_set(cache_key, body)
    return body


async def _safe_listings(awaitable: Any) -> list[Listing]:
    try:
        result = await awaitable
        return result if isinstance(result, list) else []
    except Exception as exc:
        logger.debug("listing fan-out failed: %s", exc)
        return []


async def _safe_quotes(awaitable: Any) -> list[MarketPrice]:
    try:
        result = await awaitable
    except Exception as exc:
        logger.debug("market quote fan-out failed: %s", exc)
        return []
    if isinstance(result, list):
        return [x for x in result if isinstance(x, MarketPrice)]
    if isinstance(result, MarketPrice):
        return [result]
    return []


def _search_actions(encoded_query: str) -> list[dict[str, Any]]:
    if not encoded_query:
        return []
    # eBay intentionally stays out of the primary fallback list while the
    # account is blocked. Keep the template above so existing eBay listing
    # rows can still derive a search_url when the provider returns data.
    primary_sources = ("tcgplayer", "cardmarket", "pricecharting", "google_shopping")
    return [
        {
            "source": source,
            "label": _provider_label(source),
            "url": _SEARCH_URL_TEMPLATE[source].format(q=encoded_query),
            "kind": "search",
        }
        for source in primary_sources
    ]


def _listing_rows(listings: list[Listing], encoded_query: str) -> list[dict[str, Any]]:
    by_source: dict[str, dict[str, Any]] = {}
    for listing in listings:
        source = (listing.source or "").lower().strip()
        if not source:
            continue
        current = by_source.get(source)
        if current is None or float(listing.price) < float(current["price"]["amount"]):
            by_source[source] = {
                "source": source,
                "label": _provider_label(source),
                "kind": "listing",
                "price_kind": "asking",
                "price": {
                    "amount": round(float(listing.price), 2),
                    "currency": listing.currency or "USD",
                },
                "market": None,
                "url": listing.url or None,
                "image_url": listing.image_url,
                "is_auction": bool(listing.is_auction),
                "updated_at": None,
                "subtitle": "Active seller listing",
                "search_url": _search_url(source, encoded_query),
            }
    return list(by_source.values())


def _market_price_rows(
    quotes: list[MarketPrice], encoded_query: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for quote in quotes:
        source = (quote.source or "").lower().strip()
        amount = quote.market or quote.low or quote.mid or quote.high
        if not source or amount is None:
            continue
        extras = quote.extras or {}
        price_kind = str(
            extras.get("price_kind") or ("market" if quote.market else "low")
        )
        rows.append(
            {
                "source": source,
                "label": _provider_label(source),
                "kind": "market_price",
                "price_kind": price_kind,
                "price": {
                    "amount": round(float(amount), 2),
                    "currency": quote.currency or "USD",
                },
                "market": {
                    "market": _money(quote.market, quote.currency),
                    "low": _money(quote.low, quote.currency),
                    "mid": _money(quote.mid, quote.currency),
                    "high": _money(quote.high, quote.currency),
                },
                "url": _quote_url(source, extras),
                "image_url": None,
                "is_auction": False,
                "updated_at": extras.get("as_of") or extras.get("updated_at"),
                "subtitle": _market_subtitle(source, extras),
                "search_url": _search_url(source, encoded_query),
            }
        )
    return rows


def _catalog_pricing_quote(card: dict[str, Any]) -> MarketPrice | None:
    pricing = card.get("pricing_summary") or {}
    if not isinstance(pricing, dict):
        return None
    market = _money_amount(pricing.get("market"))
    low = _money_amount(pricing.get("low"))
    mid = _money_amount(pricing.get("mid"))
    high = _money_amount(pricing.get("high"))
    if not any(v is not None for v in (market, low, mid, high)):
        return None
    sources = pricing.get("sources") or []
    source = str(sources[0]) if sources else str(card.get("source") or "catalog")
    currency = str(pricing.get("currency") or "USD")
    return MarketPrice(
        source=source,
        market=market,
        low=low,
        mid=mid,
        high=high,
        currency=currency,
        extras={
            "as_of": pricing.get("as_of"),
            "source_provider": card.get("source") or "catalog",
            "card_id": card.get("id"),
            "card_name": card.get("name"),
        },
    )


def _dedupe_quotes(quotes: list[MarketPrice]) -> list[MarketPrice]:
    by_source: dict[str, MarketPrice] = {}
    priority = {"tcgplayer": 0, "cardmarket": 0, "justtcg": 1, "pricecharting": 2}
    for quote in quotes:
        source = (quote.source or "").lower().strip()
        if not source:
            continue
        current = by_source.get(source)
        if current is None:
            by_source[source] = quote
            continue
        current_priority = priority.get((current.source or "").lower(), 10)
        next_priority = priority.get(source, 10)
        current_amount = (
            current.market or current.low or current.mid or current.high or 0
        )
        next_amount = quote.market or quote.low or quote.mid or quote.high or 0
        if next_priority < current_priority or (
            next_priority == current_priority and next_amount > 0 >= current_amount
        ):
            by_source[source] = quote
    return list(by_source.values())


def _money(amount: float | None, currency: str | None) -> dict[str, Any] | None:
    if amount is None:
        return None
    return {"amount": round(float(amount), 2), "currency": currency or "USD"}


def _money_amount(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    try:
        amount = value.get("amount")
        if amount is None:
            return None
        parsed = float(amount)
        if parsed <= 0:
            return None
        return round(parsed, 2)
    except (TypeError, ValueError):
        return None


def _quote_url(source: str, extras: dict[str, Any]) -> str | None:
    if source == "tcgplayer":
        return extras.get("tcgplayer_url") or None
    if source == "cardmarket":
        return extras.get("cardmarket_url") or None
    return extras.get("url") or extras.get("product_url") or None


def _market_subtitle(source: str, extras: dict[str, Any]) -> str:
    provider = extras.get("source_provider") or extras.get("provider")
    variant = extras.get("variant") or extras.get("sub_type")
    bits = ["Market price"]
    if variant:
        bits.append(str(variant))
    if provider and provider != source:
        bits.append(f"via {_provider_label(str(provider))}")
    return " · ".join(bits)


def _search_url(source: str, encoded_query: str) -> str | None:
    template = _SEARCH_URL_TEMPLATE.get(source)
    if template and encoded_query:
        return template.format(q=encoded_query)
    return None


def _sort_key(row: dict[str, Any]) -> tuple[int, float, str]:
    kind_rank = 0 if row.get("kind") == "listing" else 1
    amount = row.get("price", {}).get("amount")
    try:
        parsed = float(amount)
    except (TypeError, ValueError):
        parsed = 999_999_999.0
    return (kind_rank, parsed, str(row.get("source") or ""))


__all__ = ["get_marketplace_prices_for_card"]
