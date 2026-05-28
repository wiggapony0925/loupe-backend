"""YGOPRODeck API client (https://ygoprodeck.com/api-guide/) — Yu-Gi-Oh!

Wrapped in the shared ``"ygoprodeck"`` circuit breaker via
:func:`request_json`. YGOPRODeck signals "no results" as HTTP 400, so
we pass it as an ``extra_ok_status`` to keep that path from poisoning
the breaker.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.integrations._http._resilient import request_json

BASE_URL = "https://db.ygoprodeck.com/api/v7"
INTEGRATION = "ygoprodeck"


async def search_cards(query: str) -> dict[str, Any]:
    """Search Yu-Gi-Oh! cards by fname (fuzzy name)."""
    s = get_settings()
    body = await request_json(
        integration=INTEGRATION,
        method="GET",
        url=f"{BASE_URL}/cardinfo.php",
        params={"fname": query},
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
        extra_ok_statuses=(400,),
    )
    return body or {"data": []}


async def get_card(card_id: int) -> dict[str, Any] | None:
    """Fetch a single Yu-Gi-Oh! card by numeric ID."""
    s = get_settings()
    body = await request_json(
        integration=INTEGRATION,
        method="GET",
        url=f"{BASE_URL}/cardinfo.php",
        params={"id": card_id},
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
        not_found_ok=True,
        extra_ok_statuses=(400,),
    )
    if body is None:
        return None
    data = body.get("data") or []
    return data[0] if data else None


async def list_sets() -> list[dict[str, Any]]:
    """List Yu-Gi-Oh! set metadata."""
    s = get_settings()
    body = await request_json(
        integration=INTEGRATION,
        method="GET",
        url=f"{BASE_URL}/cardsets.php",
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
    )
    return list(body) if isinstance(body, list) else []


__all__ = ["get_card", "list_sets", "search_cards"]
