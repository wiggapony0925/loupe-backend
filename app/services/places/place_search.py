"""Place lookup for the profile's location field — free, keyed to nothing.

Location was a free-text box, so profiles carried "whatever" — unusable for
anything (no grouping, no "collectors near me", no trust signal) and easy to
abuse. This turns it into a CHOICE from a real gazetteer.

Source is Open-Meteo's geocoding API: no key, no billing, no attribution
requirement, and it returns the admin hierarchy (city → region → country)
that a picker needs. Same call as the Overpass card-shop locator — a free
upstream, bounded and cached so we stay a good citizen.

The backend owns the DISPLAY STRING ("Berlin, Germany"), per the house rule
that clients render server text verbatim: otherwise web and mobile would
each invent their own formatting and the same city would read two ways.
"""

from __future__ import annotations

import json

import httpx

from app.platform.cache_l2 import kv_get, kv_set
from app.schemas.places import PlaceSuggestion, PlaceSuggestions
from app.utils.logger import get_logger

logger = get_logger("services.places")

SEARCH_URL = "https://geocoding-api.open-meteo.com/v1/search"
TIMEOUT_S = 6.0
CACHE_TTL_S = 30 * 24 * 3600  # place names don't move
MAX_RESULTS = 8
#: Below this a query matches half the planet and the picker is noise.
MIN_QUERY = 2


def _cache_key(q: str) -> str:
    return f"places:v1:{q}"


def _label(row: dict) -> str:
    """ "Berlin, Germany" / "Austin, Texas, United States".

    Region is included only when it disambiguates — "Berlin, Berlin, Germany"
    reads like a bug, and a city that shares its name with its region is
    common enough to be worth the check.
    """
    name = (row.get("name") or "").strip()
    region = (row.get("admin1") or "").strip()
    country = (row.get("country") or "").strip()
    parts = [
        p for p in (name, region if region and region != name else "", country) if p
    ]
    return ", ".join(parts)


async def search(q: str) -> PlaceSuggestions:
    """Up to ``MAX_RESULTS`` places matching ``q``. Never raises upstream."""
    needle = q.strip().lower()
    if len(needle) < MIN_QUERY:
        return PlaceSuggestions(places=[])

    key = _cache_key(needle)
    cached = await kv_get(key)
    if cached is not None:
        try:
            rows = json.loads(cached)
            return PlaceSuggestions(places=[PlaceSuggestion(**r) for r in rows])
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("bad cached place payload for %r; refetching", needle)

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(
                SEARCH_URL,
                params={"name": needle, "count": MAX_RESULTS, "format": "json"},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        # A picker that can't reach its gazetteer must not block saving a
        # profile — the client falls back to accepting what was typed.
        logger.warning("place search failed for %r: %s", needle, exc)
        return PlaceSuggestions(places=[], degraded=True)

    seen: set[str] = set()
    places: list[PlaceSuggestion] = []
    for row in payload.get("results") or []:
        label = _label(row)
        # The gazetteer returns near-duplicates (same city, several ids).
        if not label or label in seen:
            continue
        seen.add(label)
        places.append(
            PlaceSuggestion(
                label=label,
                city=(row.get("name") or "").strip() or None,
                region=(row.get("admin1") or "").strip() or None,
                country=(row.get("country") or "").strip() or None,
                country_code=(row.get("country_code") or "").strip().upper() or None,
            )
        )

    await kv_set(
        key,
        json.dumps([p.model_dump() for p in places]),
        ttl_seconds=CACHE_TTL_S,
    )
    return PlaceSuggestions(places=places)


__all__ = ["search"]
