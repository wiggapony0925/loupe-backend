"""Card-set listing endpoint (public live proxy).

``GET /sets?tcg=<pokemon|magic|yugioh|all>`` returns the upstream provider's
set catalog through a 24-hour cache. Unauthenticated.

``GET /sets/progress`` is user-scoped and returns set-completion progress
for every set the signed-in user owns at least one card from.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.services.catalog import card_search_service, game_registry
from app.services.collection import set_progress_service

router = APIRouter(prefix="/sets", tags=["sets"])


@router.get(
    "",
    summary="List card sets (public)",
    description=(
        "`sort=newest` orders by release date descending (undated sets last); "
        "the default keeps the provider's natural order. `tcg=all` merges the "
        "date-backed games (Pokémon/Magic/Yu-Gi-Oh!) and is always "
        'newest-first — the feed behind the "Newest sets" discovery rail. '
        "`limit` truncates after sorting; `total` stays the full count."
    ),
)
async def list_sets(
    tcg: str = Query("magic", pattern=game_registry.tcg_pattern(supported_only=True)),
    sort: str = Query("catalog", pattern="^(catalog|newest)$"),
    limit: int | None = Query(None, ge=1, le=500),
) -> dict[str, Any]:
    return await card_search_service.list_sets(tcg, sort=sort, limit=limit)


@router.get(
    "/progress",
    summary="Set-completion progress for the signed-in user",
    description=(
        "Returns one entry per set the user owns at least one card from, "
        "sorted by completion percent (highest first). Each entry: "
        "`{setId, setName, setCode, tcg, imageUrl, owned, total, percent, "
        "estimatedValueUsd, missingTop: [{cardId, name, number, imageUrl}]}`. "
        "All values are computed from real graded-card data; sets with "
        "unknown total fall back to the count of cards we have indexed."
    ),
)
async def get_progress(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await set_progress_service.list_progress(db, user)


@router.get(
    "/{set_id}/checklist",
    summary="Full card checklist for one set (owned + still-missing)",
    description=(
        "The complete card list for a set — every card flagged `owned` or not "
        "— so the client can render a 'you have these / still missing these' "
        "sheet. Shape: `{setId, setName, total, owned, cards: [{id, name, "
        "number, imageUrl, owned}]}`. The full list comes from the catalog "
        "mirror; `owned` is true when the signed-in user holds a copy with the "
        "same collector number in this set."
    ),
)
async def get_set_checklist(
    set_id: uuid.UUID = Path(..., description="CardSet id"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await set_progress_service.set_checklist(db, user, set_id)


__all__ = ["router"]
