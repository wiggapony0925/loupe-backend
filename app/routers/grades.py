"""Graded-card endpoints (the user's collection of grades)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.card import Card
from app.models.grade import GradedCard
from app.models.user import User
from app.schemas.grade import GradedCardCreate, GradedCardRead, GradedCardUpdate
from app.utils.time import utcnow

router = APIRouter(prefix="/grades", tags=["grades"])


def _to_read(row: GradedCard, card: Card | None) -> GradedCardRead:
    out = GradedCardRead.model_validate(row)
    if card is not None:
        out.card_name = card.name
        out.card_image_url = card.image_url
        out.card_number = card.number
    return out


@router.get("", response_model=list[GradedCardRead], summary="List my graded cards")
async def list_mine(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> list[GradedCardRead]:
    pairs = (
        await db.execute(
            select(GradedCard, Card)
            .outerjoin(Card, Card.id == GradedCard.card_id)
            .where(GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None))
            .order_by(GradedCard.graded_at.desc())
        )
    ).all()
    return [_to_read(g, c) for (g, c) in pairs]


@router.post(
    "",
    response_model=GradedCardRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a graded-card record",
)
async def create(
    payload: GradedCardCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> GradedCardRead:
    row = GradedCard(
        user_id=user.id,
        card_id=payload.card_id,
        scan_job_id=payload.scan_job_id,
        grade=payload.grade,
        house=payload.house,
        subgrades=payload.subgrades,
        estimated_value_usd=payload.estimated_value_usd,
        notes=payload.notes,
        fingerprint_hash=payload.fingerprint_hash,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return GradedCardRead.model_validate(row)


@router.get("/{grade_id}", response_model=GradedCardRead, summary="Get one graded card")
async def get_one(
    grade_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> GradedCardRead:
    pair = (
        await db.execute(
            select(GradedCard, Card)
            .outerjoin(Card, Card.id == GradedCard.card_id)
            .where(
                GradedCard.id == grade_id,
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
            )
        )
    ).first()
    if pair is None:
        raise HTTPException(status_code=404, detail="Graded card not found")
    return _to_read(pair[0], pair[1])


@router.patch(
    "/{grade_id}", response_model=GradedCardRead, summary="Update notes/value"
)
async def update(
    grade_id: uuid.UUID,
    payload: GradedCardUpdate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> GradedCardRead:
    row = (
        await db.execute(
            select(GradedCard).where(
                GradedCard.id == grade_id,
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Graded card not found")
    if payload.notes is not None:
        row.notes = payload.notes
    if payload.estimated_value_usd is not None:
        row.estimated_value_usd = payload.estimated_value_usd
    await db.commit()
    await db.refresh(row)
    return GradedCardRead.model_validate(row)


@router.delete("/{grade_id}", status_code=status.HTTP_204_NO_CONTENT)
async def soft_delete(
    grade_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = (
        await db.execute(
            select(GradedCard).where(
                GradedCard.id == grade_id,
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Graded card not found")
    row.deleted_at = utcnow()
    await db.commit()


__all__ = ["router"]
