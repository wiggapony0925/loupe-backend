"""Admin card explorer — search local catalog, inspect a card's full record
(provider refs + price ladder), and record a manual price override.

Searches the *local* ``cards`` table (materialised catalog) rather than fanning
out to upstream providers, so it's fast and reflects exactly what the app holds.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.card_external_ref import CardExternalRef
from app.models.enums import GradeHouseEnum, PriceSourceEnum
from app.models.price import PriceSnapshot
from app.schemas.card_admin import (
    AdminCardDetail,
    AdminCardPage,
    AdminCardRow,
    ExternalRefRead,
    PriceOverrideRequest,
    PriceSnapshotRead,
)

_MAX_PRICES = 50


def _row(card: Card, set_name: str | None) -> AdminCardRow:
    return AdminCardRow(
        id=card.id,
        name=card.name,
        set_name=set_name,
        number=card.number,
        tcg=card.tcg.value if hasattr(card.tcg, "value") else str(card.tcg),
        rarity=card.rarity,
        year=card.year,
        image_url=card.image_url,
    )


async def search(
    db: AsyncSession, *, q: str | None, page: int = 1, page_size: int = 25
) -> AdminCardPage:
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    base = select(Card, CardSet.name).join(CardSet, Card.set_id == CardSet.id)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        base = base.where(
            or_(
                func.lower(Card.name).like(like),
                func.lower(Card.number).like(like),
                func.lower(CardSet.name).like(like),
            )
        )

    total = await db.scalar(
        select(func.count()).select_from(base.order_by(None).subquery())
    )
    rows = (
        await db.execute(
            base.order_by(Card.name).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    return AdminCardPage(
        results=[_row(card, set_name) for card, set_name in rows],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


async def _get_card(db: AsyncSession, card_id: uuid.UUID) -> tuple[Card, str | None]:
    row = (
        await db.execute(
            select(Card, CardSet.name)
            .join(CardSet, Card.set_id == CardSet.id)
            .where(Card.id == card_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Card not found")
    return row[0], row[1]


async def get_detail(db: AsyncSession, card_id: uuid.UUID) -> AdminCardDetail:
    card, set_name = await _get_card(db, card_id)

    refs = (
        (
            await db.execute(
                select(CardExternalRef).where(CardExternalRef.card_id == card_id)
            )
        )
        .scalars()
        .all()
    )
    prices = (
        (
            await db.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.card_id == card_id)
                .order_by(
                    PriceSnapshot.sale_date.desc().nulls_last(),
                    PriceSnapshot.created_at.desc(),
                )
                .limit(_MAX_PRICES)
            )
        )
        .scalars()
        .all()
    )

    return AdminCardDetail(
        **_row(card, set_name).model_dump(),
        set_id=card.set_id,
        image_phash=card.image_phash,
        card_metadata=card.card_metadata,
        external_refs=[
            ExternalRefRead(
                source=r.source,
                external_id=r.external_id,
                confidence=float(r.confidence) if r.confidence is not None else None,
            )
            for r in refs
        ],
        prices=[
            PriceSnapshotRead(
                id=p.id,
                house=p.house.value if hasattr(p.house, "value") else str(p.house),
                grade=float(p.grade),
                source=p.source.value if hasattr(p.source, "value") else str(p.source),
                price_usd=float(p.price_usd),
                sale_date=p.sale_date,
                created_at=p.created_at,
            )
            for p in prices
        ],
    )


async def add_price_override(
    db: AsyncSession, card_id: uuid.UUID, payload: PriceOverrideRequest
) -> PriceSnapshotRead:
    """Record a manual price point — an append-only `manual`-source snapshot
    that flows into the card's price ladder like any other source."""
    await _get_card(db, card_id)  # 404s if the card doesn't exist
    snap = PriceSnapshot(
        card_id=card_id,
        house=GradeHouseEnum(payload.house),
        grade=Decimal(str(payload.grade)),
        source=PriceSourceEnum.manual,
        price_usd=Decimal(str(payload.price_usd)),
        sale_date=payload.sale_date or date.today(),
    )
    db.add(snap)
    await db.commit()
    await db.refresh(snap)
    return PriceSnapshotRead(
        id=snap.id,
        house=snap.house.value,
        grade=float(snap.grade),
        source=snap.source.value,
        price_usd=float(snap.price_usd),
        sale_date=snap.sale_date,
        created_at=snap.created_at or datetime.now(),
    )


__all__ = ["add_price_override", "get_detail", "search"]
