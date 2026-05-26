"""Pokémon TCG API client (https://pokemontcg.io)."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("clients.pokemon_tcg")

BASE_URL = "https://api.pokemontcg.io/v2"


def _headers() -> dict[str, str]:
    s = get_settings()
    headers = {"Accept": "application/json"}
    if s.pokemon_tcg_api_key:
        headers["X-Api-Key"] = s.pokemon_tcg_api_key
    return headers


async def search_cards(
    query: str, page: int = 1, page_size: int = 25
) -> dict[str, Any]:
    """Search cards by name/number (Pokémon TCG ``q`` syntax)."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
        resp = await client.get(
            f"{BASE_URL}/cards",
            params={"q": query, "page": page, "pageSize": page_size},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def get_card(card_id: str) -> dict[str, Any] | None:
    """Fetch a single card by its Pokémon TCG ID."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
        resp = await client.get(f"{BASE_URL}/cards/{card_id}", headers=_headers())
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("data")


async def list_sets() -> list[dict[str, Any]]:
    """List all Pokémon TCG sets."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
        resp = await client.get(f"{BASE_URL}/sets", headers=_headers())
        resp.raise_for_status()
        return list(resp.json().get("data", []))


__all__ = ["get_card", "list_sets", "search_cards"]
