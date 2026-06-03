"""Sealed-product catalog + holdings persistence.

Extracted from :mod:`app.routers.collection.sealed` so the routers stay
thin. Catalog operations are read-only over :class:`SealedProduct`;
holdings operations are user-scoped CRUD over :class:`SealedHolding`
with soft-delete semantics (matches graded-card behavior).
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SealedProductTypeEnum, TcgEnum
from app.models.sealed import SealedHolding, SealedProduct
from app.models.user import User
from app.schemas.sealed import (
    SealedHoldingCreate,
    SealedHoldingRead,
    SealedHoldingUpdate,
)
from app.utils.time import utcnow

SortKey = Literal["recent", "oldest", "value_desc", "value_asc"]

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


def to_read(row: SealedHolding, product: SealedProduct | None) -> SealedHoldingRead:
    """Project a holding row + its catalog row into the read schema."""
    out = SealedHoldingRead.model_validate(row)
    if product is not None:
        out.product_name = product.name
        out.product_image_url = product.image_url
        out.product_type = product.product_type
        out.product_tcg = product.tcg
        out.product_set_name = product.set_name
    return out


# ── Catalog ────────────────────────────────────────────────────────────────


async def search_catalog(
    db: AsyncSession,
    *,
    q: str | None,
    tcg: TcgEnum | None,
    product_type: SealedProductTypeEnum | None,
    limit: int,
    cursor: int,
) -> list[SealedProduct]:
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
    # Newest releases first so discovery surfaces feel fresh, fallback
    # to name for deterministic paging on undated rows.
    stmt = (
        stmt.order_by(
            SealedProduct.release_date.desc().nulls_last(),
            SealedProduct.name.asc(),
        )
        .offset(cursor)
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_catalog_item(db: AsyncSession, product_id: uuid.UUID) -> SealedProduct:
    row = await db.get(SealedProduct, product_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Sealed product not found")
    return row


# ── Holdings ───────────────────────────────────────────────────────────────


async def list_holdings(
    db: AsyncSession,
    user: User,
    *,
    include_opened: bool,
    sort: SortKey,
    limit: int,
    cursor: int,
) -> list[tuple[SealedHolding, SealedProduct]]:
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
    return [(h, p) for h, p in (await db.execute(stmt)).all()]


async def _load_owned(
    db: AsyncSession, user: User, holding_id: uuid.UUID
) -> SealedHolding:
    row = await db.get(SealedHolding, holding_id)
    if row is None or row.user_id != user.id or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Sealed holding not found")
    return row


async def create_holding(
    db: AsyncSession, user: User, payload: SealedHoldingCreate
) -> tuple[SealedHolding, SealedProduct]:
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
    return row, product


async def update_holding(
    db: AsyncSession,
    user: User,
    holding_id: uuid.UUID,
    payload: SealedHoldingUpdate,
) -> tuple[SealedHolding, SealedProduct | None]:
    row = await _load_owned(db, user, holding_id)
    if payload.quantity is not None:
        row.quantity = payload.quantity
    fields = payload.model_fields_set
    if "purchase_price_usd" in fields:
        row.purchase_price_usd = payload.purchase_price_usd
    if "purchase_date" in fields:
        row.purchase_date = payload.purchase_date
    if "estimated_value_usd" in fields:
        row.estimated_value_usd = payload.estimated_value_usd
    if "notes" in fields:
        row.notes = payload.notes
    if "opened_at" in fields:
        row.opened_at = payload.opened_at
    await db.commit()
    await db.refresh(row)
    product = await db.get(SealedProduct, row.product_id)
    return row, product


async def delete_holding(db: AsyncSession, user: User, holding_id: uuid.UUID) -> None:
    row = await _load_owned(db, user, holding_id)
    row.deleted_at = utcnow()
    await db.commit()


__all__ = [
    "SortKey",
    "create_holding",
    "delete_holding",
    "get_catalog_item",
    "list_holdings",
    "search_catalog",
    "to_read",
    "update_holding",
]
