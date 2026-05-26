"""Per-marketplace lowest-price summary for a card.

Powers the marketplace chip row on the card-detail sheet
(eBay $X · TCGplayer $Y · ...). For each provider observed in live
listings we expose the cheapest active price + a deep link to that
provider's search results so the user can browse beyond the sample we
fetched.

Built on top of :mod:`app.services.market.listings_service` so the
provider fan-out + cache are reused.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from app.services.market import listings_service

# Provider → "search URL" template. ``{q}`` is the URL-encoded query.
# Used only when no listing in the result carries an obvious search URL.
_SEARCH_URL_TEMPLATE: dict[str, str] = {
    "ebay": "https://www.ebay.com/sch/i.html?_nkw={q}&LH_BIN=1",
    "tcgplayer": "https://www.tcgplayer.com/search/all/product?q={q}",
    "130point": "https://130point.com/sales/?q={q}",
}


def _provider_label(source: str) -> str:
    s = source.lower().strip()
    return {
        "ebay": "eBay",
        "tcgplayer": "TCGplayer",
        "130point": "130point",
        "psa": "PSA",
        "cgc": "CGC",
    }.get(s, source)


async def get_marketplace_prices_for_card(
    card_id: str, *, limit: int = 50
) -> dict[str, Any] | None:
    """Return the lowest active listing per provider."""
    raw = await listings_service.get_listings_for_card(card_id, limit=limit)
    if raw is None:
        return None

    query = str(raw.get("query") or "")
    by_source: dict[str, dict[str, Any]] = {}
    for x in raw.get("listings") or []:
        source = (x.get("source") or "").lower().strip()
        if not source:
            continue
        price = x.get("price") or {}
        amount = price.get("amount")
        if not isinstance(amount, (int, float)):
            continue
        current = by_source.get(source)
        if current is None or float(amount) < float(current["price"]["amount"]):
            by_source[source] = {
                "source": source,
                "label": _provider_label(source),
                "price": {
                    "amount": round(float(amount), 2),
                    "currency": price.get("currency") or "USD",
                },
                "url": x.get("url"),
                "image_url": x.get("image_url"),
                "is_auction": bool(x.get("is_auction") or False),
            }

    # Always include a deep-link to the provider's search so users can
    # browse beyond the cheapest hit.
    encoded = quote_plus(query) if query else ""
    rows = list(by_source.values())
    for r in rows:
        template = _SEARCH_URL_TEMPLATE.get(r["source"])
        if template and encoded:
            r["search_url"] = template.format(q=encoded)
        else:
            r["search_url"] = None

    rows.sort(key=lambda r: float(r["price"]["amount"]))
    return {
        "card_id": card_id,
        "query": query,
        "providers": rows,
    }


__all__ = ["get_marketplace_prices_for_card"]
