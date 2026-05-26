"""Sealed-product catalog + ownership endpoints.

Two router groups share this module:

* ``/v1/sealed/...`` — public catalog (search + detail). Anyone hitting
  the API can browse sealed SKUs the same way they browse singles.
* ``/v1/sealed-holdings/...`` — the signed-in user's vault of sealed
  product. CRUD with soft-delete (matches graded-card semantics).

Combined here intentionally — the surface is small and splitting would
just create import noise. Each FastAPI router is exported separately so
``main.py`` can mount them under the right tags.

Both routers delegate persistence to
:mod:`app.services.collection.sealed_service`.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.enums import SealedProductTypeEnum, TcgEnum
from app.models.user import User
from app.schemas.sealed import (
    SealedHoldingCreate,
    SealedHoldingRead,
    SealedHoldingUpdate,
    SealedProductRead,
)
from app.services.collection import sealed_service

# ── Catalog router ────────────────────────────────────────────────────────

catalog_router = APIRouter(prefix="/sealed", tags=["sealed"])


@catalog_router.get(
    "/search",
    response_model=list[SealedProductRead],
    summary="Search the sealed-product catalog",
    description=(
        "Free-text search across the global sealed-product catalog "
        "(booster boxes, ETBs, tins, etc.). Optional `tcg` and "
        "`product_type` filters. Public — no auth required so unsigned "
        "browse flows work."
    ),
)
async def search_catalog(
    q: str | None = Query(None, max_length=120),
    tcg: TcgEnum | None = Query(None),
    product_type: SealedProductTypeEnum | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[SealedProductRead]:
    rows = await sealed_service.search_catalog(
        db,
        q=q,
        tcg=tcg,
        product_type=product_type,
        limit=limit,
        cursor=cursor,
    )
    return [SealedProductRead.model_validate(r) for r in rows]


@catalog_router.get(
    "/{product_id}",
    response_model=SealedProductRead,
    summary="Get a sealed-product catalog entry",
)
async def get_catalog_item(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SealedProductRead:
    row = await sealed_service.get_catalog_item(db, product_id)
    return SealedProductRead.model_validate(row)


# ── Holdings router ───────────────────────────────────────────────────────

holdings_router = APIRouter(prefix="/sealed-holdings", tags=["sealed-holdings"])


@holdings_router.get(
    "",
    response_model=list[SealedHoldingRead],
    summary="List my sealed holdings",
    description=(
        "Returns the signed-in user's sealed inventory, joined to the "
        "catalog row so the client can render product name / image "
        "without an N+1 round-trip. Soft-deleted rows are excluded; "
        "use `include_opened=false` to also hide boxes the user has "
        "marked as opened."
    ),
)
async def list_mine(
    include_opened: bool = Query(True),
    sort: Literal["recent", "oldest", "value_desc", "value_asc"] = Query("recent"),
    limit: int = Query(500, ge=1, le=1000),
    cursor: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[SealedHoldingRead]:
    rows = await sealed_service.list_holdings(
        db,
        user,
        include_opened=include_opened,
        sort=sort,
        limit=limit,
        cursor=cursor,
    )
    return [sealed_service.to_read(h, p) for h, p in rows]


@holdings_router.post(
    "",
    response_model=SealedHoldingRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a sealed holding",
)
async def create_holding(
    payload: SealedHoldingCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> SealedHoldingRead:
    row, product = await sealed_service.create_holding(db, user, payload)
    return sealed_service.to_read(row, product)


@holdings_router.patch(
    "/{holding_id}",
    response_model=SealedHoldingRead,
    summary="Update a sealed holding",
)
async def update_holding(
    holding_id: uuid.UUID,
    payload: SealedHoldingUpdate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> SealedHoldingRead:
    row, product = await sealed_service.update_holding(db, user, holding_id, payload)
    return sealed_service.to_read(row, product)


@holdings_router.delete(
    "/{holding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a sealed holding",
)
async def delete_holding(
    holding_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await sealed_service.delete_holding(db, user, holding_id)


__all__ = ["catalog_router", "holdings_router"]
