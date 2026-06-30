"""apitcg API client (https://docs.apitcg.com) — one provider, many games.

A single key unlocks the catalog for several TCGs (One Piece, Digimon, Dragon
Ball Fusion, Union Arena, Gundam, Star Wars, Riftbound). Endpoints are
game-scoped under ``/api/<game>/`` and share one schema family, so this client
is parametrised by an apitcg game slug — adding a new game is a one-line entry
in the caller's dispatch, not a new client.

Notes:
* Host is ``www.apitcg.com`` — the bare ``apitcg.com`` 308-redirects and our
  HTTP layer doesn't follow redirects.
* Auth is the ``x-api-key`` header. Blank key → the provider is "not
  configured" and every apitcg-backed game stays gracefully empty.
* ``/cards`` is natively paginated (``page`` + ``limit``, max 100) and returns
  ``{page, limit, total, totalPages, data: [...]}``. With no filter it pages the
  full catalog; with a property filter (e.g. ``name=``) it searches.
* ``/sets`` returns ``{data: [{id, name, series, release_date, total_cards,
  …}]}``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.integrations._http._resilient import request_json
from app.platform.api_budget import ApiBudget

BASE_URL = "https://www.apitcg.com/api"
INTEGRATION = "apitcg"

#: Hard monthly request ceiling (free tier = 1000/mo). Every upstream call is
#: metered against this; once the soft ceiling is hit the catalog layer serves
#: cached/stale data instead of calling apitcg, so we never exceed the plan.
budget = ApiBudget(INTEGRATION, get_settings().apitcg_monthly_budget)


async def remaining_budget() -> int:
    """Requests still available this month (for admin/observability)."""
    return await budget.remaining()


#: Map our internal tcg key → apitcg's game slug. Extend to light up more games.
GAME_SLUGS: dict[str, str] = {
    "onepiece": "one-piece",
    "dragonball": "dragon-ball-fusion",
    "unionarena": "union-arena",
    "gundam": "gundam",
    "starwars": "star-wars-unlimited",
    "riftbound": "riftbound",
}

_MAX_LIMIT = 100  # apitcg caps page size at 100


def is_configured() -> bool:
    return bool(get_settings().apitcg_api_key)


def _headers() -> dict[str, str]:
    return {
        "x-api-key": get_settings().apitcg_api_key or "",
        "Accept": "application/json",
    }


async def list_cards(
    slug: str,
    *,
    page: int = 1,
    limit: int = _MAX_LIMIT,
    **filters: Any,
) -> dict[str, Any]:
    """One page of a game's cards. With ``**filters`` (e.g. ``name=``) it searches;
    with none it pages the full catalog. Returns the raw apitcg envelope."""
    if not is_configured():
        return {"data": [], "total": 0, "page": page, "totalPages": 0}
    # Free-tier guard: once the monthly ceiling is hit, refuse the call so the
    # caller falls back to cached/stale data instead of exceeding the plan.
    if not await budget.can_spend():
        return {"data": [], "total": 0, "page": page, "totalPages": 0}
    params: dict[str, Any] = {"page": page, "limit": min(limit, _MAX_LIMIT)}
    params.update({k: v for k, v in filters.items() if v is not None})
    body = await request_json(
        integration=INTEGRATION,
        method="GET",
        url=f"{BASE_URL}/{slug}/cards",
        params=params,
        headers=_headers(),
        timeout_s=get_settings().http_timeout_seconds,
        not_found_ok=True,
    )
    await budget.spend()  # a real upstream response came back — count it
    if not isinstance(body, dict):
        return {"data": [], "total": 0, "page": page, "totalPages": 0}
    return body


async def list_all_cards(slug: str) -> list[dict[str, Any]]:
    """Every card for a game (raw apitcg shape), deduped by id. Pages fetched in
    parallel for a fast cold path; a failed page is skipped, not fatal. Callers
    should cache the result — the catalog is effectively static."""
    first = await list_cards(slug, page=1, limit=_MAX_LIMIT)
    raw = list(first.get("data") or [])
    total_pages = int(first.get("totalPages") or first.get("total_pages") or 1)
    if total_pages > 1:
        rest = await asyncio.gather(
            *(
                list_cards(slug, page=p, limit=_MAX_LIMIT)
                for p in range(2, total_pages + 1)
            ),
            return_exceptions=True,
        )
        for r in rest:
            if isinstance(r, dict):
                raw += list(r.get("data") or [])
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in raw:
        cid = str(c.get("id") or "")
        if cid and cid not in seen:
            seen.add(cid)
            out.append(c)
    return out


async def search_cards(slug: str, name: str) -> list[dict[str, Any]]:
    body = await list_cards(slug, name=name, limit=_MAX_LIMIT)
    return body.get("data") or []


async def get_card(slug: str, card_id: str) -> dict[str, Any] | None:
    body = await list_cards(slug, id=card_id, limit=1)
    data = body.get("data") or []
    for c in data:
        if str(c.get("id")) == card_id:
            return c
    return data[0] if data else None


async def list_sets(slug: str) -> list[dict[str, Any]]:
    if not is_configured():
        return []
    if not await budget.can_spend():
        return []
    body = await request_json(
        integration=INTEGRATION,
        method="GET",
        url=f"{BASE_URL}/{slug}/sets",
        headers=_headers(),
        timeout_s=get_settings().http_timeout_seconds,
        not_found_ok=True,
    )
    await budget.spend()
    if isinstance(body, dict):
        return body.get("data") or []
    return body if isinstance(body, list) else []


__all__ = [
    "GAME_SLUGS",
    "get_card",
    "is_configured",
    "list_all_cards",
    "list_cards",
    "list_sets",
    "search_cards",
]
