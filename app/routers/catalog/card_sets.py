"""Card-set listing endpoint (public live proxy).

``GET /sets?tcg=<pokemon|magic|yugioh|all>`` returns the upstream provider's
set catalog through a 24-hour cache. Unauthenticated.

``GET /sets/progress`` is user-scoped and returns set-completion progress
for every set the signed-in user owns at least one card from.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.services.catalog import card_search_service
from app.services.collection import set_progress_service

router = APIRouter(prefix="/sets", tags=["sets"])


@router.get("", summary="List card sets (public)")
async def list_sets(
    tcg: str = Query("magic", pattern="^(pokemon|magic|yugioh|digimon|all)$"),
) -> dict[str, Any]:
    return await card_search_service.list_sets(tcg)


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


__all__ = ["router"]
