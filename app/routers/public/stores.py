"""Public card-shop locator — ``/v1/public/stores/*``.

No auth: store locations are public data, and the mobile map screen may be
opened before sign-in. The rate limit + the per-grid-cell cache inside the
service keep the free Overpass upstream comfortably within fair use.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.platform.rate_limit import rate_limit
from app.schemas.stores import NearbyStoresRead
from app.services.stores import store_locator

router = APIRouter(prefix="/public/stores", tags=["public"])

# Map pans re-query as the user explores; 30/min per client is plenty and
# caps what a hostile client can relay to Overpass through us.
stores_limit = rate_limit(limit=30, window_seconds=60, name="stores.nearby")


@router.get(
    "/nearby",
    response_model=NearbyStoresRead,
    summary="Card & game shops near a point (server-ranked, cached)",
    dependencies=[Depends(stores_limit)],
)
async def stores_nearby(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(25, ge=1, le=50),
) -> NearbyStoresRead:
    return await store_locator.nearby_stores(lat, lng, radius_km)


__all__ = ["router"]
