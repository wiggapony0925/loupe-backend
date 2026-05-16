"""Scryfall API client (https://scryfall.com/docs/api) — Magic: the Gathering."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings

BASE_URL = "https://api.scryfall.com"


async def search_cards(query: str, page: int = 1) -> dict[str, Any]:
    """Search MTG cards via Scryfall's ``/cards/search``."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
        resp = await client.get(
            f"{BASE_URL}/cards/search",
            params={"q": query, "page": page},
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 404:
            return {"data": [], "total_cards": 0, "has_more": False}
        resp.raise_for_status()
        return resp.json()


async def get_card(scryfall_id: str) -> dict[str, Any] | None:
    """Fetch a single MTG card by Scryfall ID."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
        resp = await client.get(
            f"{BASE_URL}/cards/{scryfall_id}",
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


async def list_sets() -> list[dict[str, Any]]:
    """List all MTG sets known to Scryfall."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
        resp = await client.get(
            f"{BASE_URL}/sets",
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return list(resp.json().get("data", []))


__all__ = ["get_card", "list_sets", "search_cards"]
