"""Scryfall API client (https://scryfall.com/docs/api) — Magic: the Gathering.

Wrapped in the shared ``"scryfall"`` circuit breaker via
:func:`request_json`. A flaky scryfall.com short-circuits within seconds
instead of throttling every fan-out caller.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.integrations._http._resilient import request_json

BASE_URL = "https://api.scryfall.com"
INTEGRATION = "scryfall"


async def search_cards(query: str, page: int = 1) -> dict[str, Any]:
    """Search MTG cards via Scryfall's ``/cards/search``.

    Scryfall returns a 404 when *no cards match*; that's a clean empty
    result for the caller, so we treat it as a breaker success and
    return the canonical empty envelope.
    """
    s = get_settings()
    body = await request_json(
        integration=INTEGRATION,
        method="GET",
        url=f"{BASE_URL}/cards/search",
        params={"q": query, "page": page},
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
        not_found_ok=True,
    )
    if body is None:
        return {"data": [], "total_cards": 0, "has_more": False}
    return body


async def get_card(scryfall_id: str) -> dict[str, Any] | None:
    """Fetch a single MTG card by Scryfall ID."""
    s = get_settings()
    return await request_json(
        integration=INTEGRATION,
        method="GET",
        url=f"{BASE_URL}/cards/{scryfall_id}",
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
        not_found_ok=True,
    )


async def list_sets() -> list[dict[str, Any]]:
    """List all MTG sets known to Scryfall."""
    s = get_settings()
    body = await request_json(
        integration=INTEGRATION,
        method="GET",
        url=f"{BASE_URL}/sets",
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
    )
    return list((body or {}).get("data", []))


__all__ = ["get_card", "list_sets", "search_cards"]
