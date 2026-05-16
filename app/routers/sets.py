"""Card-set listing endpoint (public live proxy).

``GET /sets?tcg=<pokemon|magic|yugioh|all>`` returns the upstream provider's
set catalog through a 24-hour cache. Unauthenticated.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.services import card_search_service

router = APIRouter(prefix="/sets", tags=["sets"])


@router.get("", summary="List card sets (public)")
async def list_sets(
    tcg: str = Query("magic", pattern="^(pokemon|magic|yugioh|all)$"),
) -> dict[str, Any]:
    return await card_search_service.list_sets(tcg)


__all__ = ["router"]
