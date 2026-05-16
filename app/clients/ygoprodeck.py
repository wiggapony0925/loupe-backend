"""YGOPRODeck API client (https://ygoprodeck.com/api-guide/) — Yu-Gi-Oh!"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings

BASE_URL = "https://db.ygoprodeck.com/api/v7"


async def search_cards(query: str) -> dict[str, Any]:
    """Search Yu-Gi-Oh! cards by fname (fuzzy name)."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
        resp = await client.get(
            f"{BASE_URL}/cardinfo.php",
            params={"fname": query},
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 400:
            return {"data": []}
        resp.raise_for_status()
        return resp.json()


async def get_card(card_id: int) -> dict[str, Any] | None:
    """Fetch a single Yu-Gi-Oh! card by numeric ID."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
        resp = await client.get(
            f"{BASE_URL}/cardinfo.php",
            params={"id": card_id},
            headers={"Accept": "application/json"},
        )
        if resp.status_code in (400, 404):
            return None
        resp.raise_for_status()
        data = resp.json().get("data") or []
        return data[0] if data else None


async def list_sets() -> list[dict[str, Any]]:
    """List Yu-Gi-Oh! set metadata."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
        resp = await client.get(
            f"{BASE_URL}/cardsets.php",
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return list(data) if isinstance(data, list) else []


__all__ = ["get_card", "list_sets", "search_cards"]
