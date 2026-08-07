"""Place autocomplete — ``/v1/public/places``.

Public because the profile editor needs it before anything about the user
matters, and place names are not private data. Rate-limited and cached so
the free upstream stays free.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.platform.rate_limit import rate_limit
from app.schemas.places import PlaceSuggestions
from app.services.places import place_search

router = APIRouter(prefix="/public/places", tags=["public"])

# Typeahead fires per keystroke on the client's debounce; this bounds what
# one client can relay upstream through us.
places_limit = rate_limit(limit=60, window_seconds=60, name="places.search")


@router.get(
    "/search",
    response_model=PlaceSuggestions,
    summary="Cities, regions and countries matching a query",
    dependencies=[Depends(places_limit)],
)
async def search_places(
    q: str = Query(..., min_length=1, max_length=80),
) -> PlaceSuggestions:
    """Empty list for a too-short query; ``degraded`` when the gazetteer is
    unreachable, so the client can fall back to free text instead of
    blocking a profile save."""
    return await place_search.search(q)


__all__ = ["router"]
