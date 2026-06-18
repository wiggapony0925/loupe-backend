"""Pokémon TCG API client (https://pokemontcg.io).

All outbound calls go through :func:`request_json`, which adds circuit
breaking, expected-404 handling, and structured failure semantics. A
sustained pokemontcg.io outage will trip the ``"pokemontcg"`` breaker
after a handful of failures and instantly short-circuit further calls
until it recovers — see :mod:`app.platform.circuit_breaker`.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.integrations._http._resilient import request_json
from app.utils.logger import get_logger

logger = get_logger("clients.pokemon_tcg")

BASE_URL = "https://api.pokemontcg.io/v2"
INTEGRATION = "pokemontcg"


def _headers() -> dict[str, str]:
    s = get_settings()
    headers = {"Accept": "application/json"}
    if s.pokemon_tcg_api_key:
        headers["X-Api-Key"] = s.pokemon_tcg_api_key
    return headers


async def search_cards(
    query: str,
    page: int = 1,
    page_size: int = 25,
    *,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Search cards by name/number (Pokémon TCG ``q`` syntax).

    ``timeout_s`` overrides the default request budget — callers inside a
    tight fan-out (e.g. card-detail market price) pass a small value so a
    slow upstream can't blow the aggregation deadline.
    """
    s = get_settings()
    body = await request_json(
        integration=INTEGRATION,
        method="GET",
        url=f"{BASE_URL}/cards",
        params={"q": query, "page": page, "pageSize": page_size},
        headers=_headers(),
        timeout_s=timeout_s if timeout_s is not None else s.http_timeout_seconds,
    )
    return body or {"data": []}


async def get_card(card_id: str) -> dict[str, Any] | None:
    """Fetch a single card by its Pokémon TCG ID."""
    s = get_settings()
    body = await request_json(
        integration=INTEGRATION,
        method="GET",
        url=f"{BASE_URL}/cards/{card_id}",
        headers=_headers(),
        timeout_s=s.http_timeout_seconds,
        not_found_ok=True,
    )
    if body is None:
        return None
    return body.get("data")


async def list_sets() -> list[dict[str, Any]]:
    """List all Pokémon TCG sets."""
    s = get_settings()
    body = await request_json(
        integration=INTEGRATION,
        method="GET",
        url=f"{BASE_URL}/sets",
        headers=_headers(),
        timeout_s=s.http_timeout_seconds,
    )
    return list((body or {}).get("data", []))


__all__ = ["get_card", "list_sets", "search_cards"]
