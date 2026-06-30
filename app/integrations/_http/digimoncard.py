"""digimoncard.io API client (https://documenter.getpostman.com/view/14059948) — Digimon.

Free, key-less public API. ``search.php`` returns a JSON array of cards, or a
``{"error": "..."}`` object when nothing matches — so callers must treat a
non-list body as "no results". Card art is not in the payload; it lives at a
deterministic URL keyed by the card id (e.g. ``BT17-017`` →
``…/images/cards/BT17-017.jpg``).

Wrapped in the shared ``"digimoncard"`` circuit breaker via ``request_json``.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.integrations._http._resilient import request_json

BASE_URL = "https://digimoncard.io/api-public"
IMAGE_BASE = "https://images.digimoncard.io/images/cards"
INTEGRATION = "digimoncard"

#: The only series the public API exposes; passing it returns the full catalog.
_SERIES = "Digimon Card Game"


def image_url(card_id: str | None) -> str | None:
    """Deterministic card-art URL for a digimoncard.io card id."""
    if not card_id:
        return None
    return f"{IMAGE_BASE}/{card_id}.jpg"


async def _search(params: dict[str, Any]) -> list[dict[str, Any]]:
    s = get_settings()
    body = await request_json(
        integration=INTEGRATION,
        # The ``.php`` path 301-redirects to the extension-less permalink, and
        # our HTTP layer doesn't follow redirects — hit the canonical URL.
        method="GET",
        url=f"{BASE_URL}/search",
        params=params,
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
    )
    # The API returns a bare list, or {"error": "..."} when nothing matched.
    return body if isinstance(body, list) else []


async def list_all() -> list[dict[str, Any]]:
    """The full Digimon catalog, name-sorted, one entry per card id.

    The API returns a row per *printing* (alt-art / rarity variants share an id),
    so we dedupe by id to a single canonical card. Cache aggressively.
    """
    raw = await _search({"series": _SERIES, "sort": "name", "sortdirection": "asc"})
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in raw:
        cid = str(c.get("id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(c)
    return out


async def search_cards(query: str) -> list[dict[str, Any]]:
    """Search Digimon cards by (fuzzy) name."""
    return await _search({"n": query, "series": _SERIES})


async def get_card(card_id: str) -> dict[str, Any] | None:
    """Fetch a single Digimon card by its printed id (e.g. ``BT17-017``)."""
    results = await _search({"card": card_id, "series": _SERIES})
    for c in results:
        if str(c.get("id")) == card_id:
            return c
    return results[0] if results else None


__all__ = ["get_card", "image_url", "list_all", "search_cards"]
