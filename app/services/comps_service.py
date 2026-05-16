"""Sold-comps service — resolves card → fan-out → post-filter by grade/house."""

from __future__ import annotations

import json
from typing import Any

from app.clients.redis_client import get_redis
from app.integrations import get_registry
from app.services import card_search_service
from app.services.listings_service import _build_query
from app.utils.logger import get_logger

logger = get_logger("services.comps")

_CACHE_PREFIX = "loupe:cards:comps"
_CACHE_TTL = 300  # 5 min


async def _cache_get(key: str) -> Any | None:
    try:
        r = await get_redis()
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:  # pragma: no cover
        logger.debug("comps cache_get failed: %s", exc)
        return None


async def _cache_set(key: str, value: Any) -> None:
    try:
        r = await get_redis()
        await r.setex(key, _CACHE_TTL, json.dumps(value, default=str))
    except Exception as exc:  # pragma: no cover
        logger.debug("comps cache_set failed: %s", exc)


async def get_comps_for_card(
    card_id: str,
    *,
    days: int = 90,
    grade: str | None = None,
    house: str | None = None,
    limit: int = 50,
) -> dict[str, Any] | None:
    cache_key = f"{_CACHE_PREFIX}:{card_id}:{days}:{limit}"
    cached = await _cache_get(cache_key)
    if cached is None:
        card = await card_search_service.get_card(card_id)
        if card is None:
            return None
        query = _build_query(card)
        registry = get_registry()
        comps = await registry.fan_out_comps(query, days=days, limit=limit)
        cached = {
            "card_id": card_id,
            "query": query,
            "days": days,
            "comps": [
                {
                    "source": c.source,
                    "title": c.title,
                    "price": {"amount": c.price, "currency": c.currency},
                    "sold_at": c.sold_at,
                    "condition": c.condition,
                    "grade": c.grade,
                    "house": c.house,
                    "url": c.url,
                    "image_url": c.image_url,
                }
                for c in comps
            ],
        }
        await _cache_set(cache_key, cached)

    # Post-filter (do NOT cache filtered variants).
    rows = cached.get("comps") or []
    if grade:
        g_norm = str(grade).strip()
        rows = [r for r in rows if str(r.get("grade") or "") == g_norm]
    if house:
        h_norm = house.lower().strip()
        rows = [r for r in rows if str(r.get("house") or "").lower() == h_norm]
    out = dict(cached)
    out["comps"] = rows
    out["filters"] = {"grade": grade, "house": house}
    return out


__all__ = ["get_comps_for_card"]
