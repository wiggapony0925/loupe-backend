"""Listings service — resolves card → search query → provider fan-out."""

from __future__ import annotations

import json
from typing import Any

from app.integrations import get_registry
from app.platform.redis_client import get_redis
from app.services.catalog import card_search_service
from app.utils.logger import get_logger

logger = get_logger("services.listings")

_CACHE_PREFIX = "loupe:cards:listings"
_CACHE_TTL = 60  # seconds


def _build_query(card: dict[str, Any]) -> str:
    parts: list[str] = []
    name = card.get("name")
    if name:
        parts.append(str(name))
    set_name = card.get("set_name") or (card.get("set") or {}).get("name")
    if set_name:
        parts.append(str(set_name))
    number = card.get("number")
    if number:
        parts.append(f"#{number}")
    return " ".join(parts).strip()


async def _cache_get(key: str) -> Any | None:
    try:
        r = await get_redis()
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:  # pragma: no cover
        logger.debug("listings cache_get failed: %s", exc)
        return None


async def _cache_set(key: str, value: Any) -> None:
    try:
        r = await get_redis()
        await r.setex(key, _CACHE_TTL, json.dumps(value, default=str))
    except Exception as exc:  # pragma: no cover
        logger.debug("listings cache_set failed: %s", exc)


async def get_listings_for_card(
    card_id: str, *, limit: int = 20
) -> dict[str, Any] | None:
    cache_key = f"{_CACHE_PREFIX}:{card_id}:{limit}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    card = await card_search_service.get_card(card_id)
    if card is None:
        return None
    query = _build_query(card)
    registry = get_registry()
    listings = await registry.fan_out_listings(query, limit=limit)
    body = {
        "card_id": card_id,
        "query": query,
        "listings": [
            {
                "source": x.source,
                "title": x.title,
                "price": {"amount": x.price, "currency": x.currency},
                "url": x.url,
                "condition": x.condition,
                "image_url": x.image_url,
                "is_auction": x.is_auction,
                "time_left_seconds": x.time_left_seconds,
            }
            for x in listings
        ],
    }
    await _cache_set(cache_key, body)
    return body


__all__ = ["get_listings_for_card"]
