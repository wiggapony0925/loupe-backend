"""Sealed-product catalog + ownership endpoints.

Two router groups share this module:

* ``/v1/sealed/...`` — public catalog (search + detail). Anyone hitting
  the API can browse sealed SKUs the same way they browse singles.
* ``/v1/sealed-holdings/...`` — the signed-in user's vault of sealed
  product. CRUD with soft-delete (matches graded-card semantics).

Combined here intentionally — the surface is small and splitting would
just create import noise. Each FastAPI router is exported separately so
``main.py`` can mount them under the right tags.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.enums import SealedProductTypeEnum, TcgEnum
from app.models.sealed import SealedHolding, SealedProduct
from app.models.user import User
from app.schemas.sealed import (
    SealedHoldingCreate,
    SealedHoldingRead,
    SealedHoldingUpdate,
    SealedProductRead,
)
from app.utils.time import utcnow

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
    stmt = select(SealedProduct)
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(SealedProduct.name).like(needle),
                func.lower(SealedProduct.set_name).like(needle),
            )
        )
    if tcg is not None:
        stmt = stmt.where(SealedProduct.tcg == tcg)
    if product_type is not None:
        stmt = stmt.where(SealedProduct.product_type == product_type)
    # Newest releases first so the home/discovery surfaces feel fresh,
    # falling back to name for deterministic paging on undated rows.
    stmt = (
        stmt.order_by(
            SealedProduct.release_date.desc().nulls_last(),
            SealedProduct.name.asc(),
        )
        .offset(cursor)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
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
    row = await db.get(SealedProduct, product_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Sealed product not found")
    return SealedProductRead.model_validate(row)


# ── Holdings router ───────────────────────────────────────────────────────

holdings_router = APIRouter(prefix="/sealed-holdings", tags=["sealed-holdings"])


def _to_read(row: SealedHolding, product: SealedProduct | None) -> SealedHoldingRead:
    out = SealedHoldingRead.model_validate(row)
    if product is not None:
        out.product_name = product.name
        out.product_image_url = product.image_url
        out.product_type = product.product_type
        out.product_tcg = product.tcg
        out.product_set_name = product.set_name
    return out


_SORT_OPTIONS: dict[str, tuple] = {
    "recent": (SealedHolding.acquired_at.desc(), SealedHolding.id.desc()),
    "oldest": (SealedHolding.acquired_at.asc(), SealedHolding.id.asc()),
    "value_desc": (
        SealedHolding.estimated_value_usd.desc().nulls_last(),
        SealedHolding.id.desc(),
    ),
    "value_asc": (
        SealedHolding.estimated_value_usd.asc().nulls_last(),
        SealedHolding.id.asc(),
    ),
}


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
    primary, tie = _SORT_OPTIONS[sort]
    stmt = (
        select(SealedHolding, SealedProduct)
        .join(SealedProduct, SealedProduct.id == SealedHolding.product_id)
        .where(SealedHolding.user_id == user.id)
        .where(SealedHolding.deleted_at.is_(None))
    )
    if not include_opened:
        stmt = stmt.where(SealedHolding.opened_at.is_(None))
    stmt = stmt.order_by(primary, tie).offset(cursor).limit(limit)
    rows = (await db.execute(stmt)).all()
    return [_to_read(h, p) for h, p in rows]


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
    product = await db.get(SealedProduct, payload.product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Sealed product not found")
    row = SealedHolding(
        user_id=user.id,
        product_id=product.id,
        quantity=payload.quantity,
        purchase_price_usd=payload.purchase_price_usd,
        purchase_date=payload.purchase_date,
        estimated_value_usd=payload.estimated_value_usd,
        notes=payload.notes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _to_read(row, product)


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
    row = await db.get(SealedHolding, holding_id)
    if row is None or row.user_id != user.id or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Sealed holding not found")
    if payload.quantity is not None:
        row.quantity = payload.quantity
    if payload.purchase_price_usd is not None:
        row.purchase_price_usd = payload.purchase_price_usd
    if payload.purchase_date is not None:
        row.purchase_date = payload.purchase_date
    if payload.estimated_value_usd is not None:
        row.estimated_value_usd = payload.estimated_value_usd
    if payload.notes is not None:
        row.notes = payload.notes
    if payload.opened_at is not None:
        row.opened_at = payload.opened_at
    await db.commit()
    await db.refresh(row)
    product = await db.get(SealedProduct, row.product_id)
    return _to_read(row, product)


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
    row = await db.get(SealedHolding, holding_id)
    if row is None or row.user_id != user.id or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Sealed holding not found")
    row.deleted_at = utcnow()
    await db.commit()


__all__ = ["catalog_router", "holdings_router"]
