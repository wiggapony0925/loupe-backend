"""Market-index endpoints — public, aggregated peer benchmarks."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.market import fx_service, market_index_service

router = APIRouter(prefix="/market", tags=["market"])


@router.get(
    "/fx/rates",
    summary="Display-currency FX table (public)",
    description=(
        "Units-per-USD conversion rates for every display currency the "
        "clients offer (fiat + crypto). ONE source of truth: web and "
        "mobile both render prices with this table, so a card is worth "
        "the same yen on every surface. Live-fetched from keyless "
        "providers, L2-cached for 12 h, degrades to a static snapshot - "
        "never errors."
    ),
)
async def get_fx_rates() -> dict[str, Any]:
    return await fx_service.get_rates()

_RangeT = Literal["1D", "1W", "1M", "3M", "YTD", "1Y", "ALL"]


@router.get(
    "/indices/{index_id}/history",
    summary="Market index history (public)",
    description=(
        "Returns a normalized (first bucket = 100) index series for the "
        "named cohort. Currently the only supported `index_id` is "
        "`psa10` — the average PSA-10 spot price across every catalog "
        "card owned by at least one Loupe user. Use as a faded baseline "
        "overlay on the portfolio chart so users can read their relative "
        "performance vs. the broader PSA-10 market."
    ),
)
async def get_index_history(
    index_id: str,
    range: _RangeT = Query("1Y"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if index_id != "psa10":
        raise HTTPException(status_code=404, detail="Unknown index")
    result = await market_index_service.psa10_index(db, range)
    return result.to_dict()
