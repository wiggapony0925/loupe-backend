"""Public card-shop locator — ``/v1/public/stores/*``.

Browsing is open (store locations are public data, and the map may be
opened before sign-in); WRITING a review needs a signed-in collector with
a claimed handle, so every review is attributable.

The rate limit + the per-grid-cell cache inside the service keep the free
Overpass upstream comfortably within fair use.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import optional_user, require_user
from app.db import get_db
from app.models.user import User
from app.platform.rate_limit import rate_limit
from app.schemas.stores import (
    NearbyStoresRead,
    SavedStoresRead,
    StoreDetailRead,
    StoreReviewRead,
    StoreReviewUpsert,
    StoreSaveRead,
)
from app.services.stores import (
    saved_stores,
    store_locator,
    store_photos,
    store_reviews,
)

router = APIRouter(prefix="/public/stores", tags=["public"])

# Map pans re-query as the user explores; 30/min per client is plenty and
# caps what a hostile client can relay to Overpass through us.
stores_limit = rate_limit(limit=30, window_seconds=60, name="stores.nearby")
review_limit = rate_limit(limit=20, window_seconds=300, name="stores.review")


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
    user: User | None = Depends(optional_user),
    db: AsyncSession = Depends(get_db),
) -> NearbyStoresRead:
    found = await store_locator.nearby_stores(lat, lng, radius_km)
    # Ratings AND the caller's saves in one query each — the map cards show
    # "★ 4.3 (12)" and a filled heart without fanning out per store.
    ratings = await store_reviews.aggregates(db, [s.id for s in found.stores])
    saved = await saved_stores.saved_ids(db, user)
    for store in found.stores:
        rating, count = ratings.get(store.id, (None, 0))
        store.rating = rating
        store.review_count = count
        store.is_saved = store.id in saved
    return found


@router.get(
    "/saved",
    response_model=SavedStoresRead,
    summary="My saved places",
)
async def saved_places(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> SavedStoresRead:
    """Newest first. Declared BEFORE /{store_id} so "saved" is never
    swallowed as a store id."""
    return SavedStoresRead(stores=await saved_stores.list_saved(db, user))


@router.get(
    "/{store_id}",
    response_model=StoreDetailRead,
    summary="One shop: details, photo, and community reviews",
    dependencies=[Depends(stores_limit)],
)
async def store_detail(
    store_id: str,
    user: User | None = Depends(optional_user),
    db: AsyncSession = Depends(get_db),
) -> StoreDetailRead:
    """404 when we've never seen the store (its area was never searched)."""
    store = await store_locator.store_by_id(store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="No such store")

    store.photo_url = await store_photos.photo_for(
        store.id,
        osm_image=store.photo_url,
        website=store.website,
        wikidata=store.wikidata_id,
    )
    rating, count = await store_reviews.aggregate(db, store_id)
    store.rating = rating
    store.review_count = count
    store.is_saved = await saved_stores.is_saved(db, user, store_id)
    return StoreDetailRead(
        store=store, reviews=await store_reviews.list_reviews(db, store_id, user)
    )


@router.put(
    "/{store_id}/review",
    response_model=StoreReviewRead,
    summary="Write or update my review of a shop",
    dependencies=[Depends(review_limit)],
)
async def upsert_store_review(
    store_id: str,
    payload: StoreReviewUpsert,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> StoreReviewRead:
    """One review per collector per store — posting again edits yours."""
    return await store_reviews.upsert_review(db, user, store_id, payload)


@router.delete(
    "/{store_id}/review",
    status_code=204,
    summary="Delete my review of a shop",
)
async def delete_store_review(
    store_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await store_reviews.delete_review(db, user, store_id)


@router.put(
    "/{store_id}/save",
    response_model=StoreSaveRead,
    summary="Save this shop to my places",
)
async def save_store(
    store_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> StoreSaveRead:
    """Idempotent — hearting twice leaves one save."""
    return StoreSaveRead(
        store_id=store_id, is_saved=await saved_stores.save(db, user, store_id)
    )


@router.delete(
    "/{store_id}/save",
    response_model=StoreSaveRead,
    summary="Remove this shop from my places",
)
async def unsave_store(
    store_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> StoreSaveRead:
    return StoreSaveRead(
        store_id=store_id, is_saved=await saved_stores.unsave(db, user, store_id)
    )


__all__ = ["router"]
