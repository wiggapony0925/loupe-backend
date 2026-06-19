"""Feature-flag CRUD + the public flag map clients gate on."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feature_flag import FeatureFlag
from app.schemas.flag import FeatureFlagCreate, FeatureFlagUpdate


async def public_map(db: AsyncSession) -> dict[str, bool]:
    """`{key: enabled}` for every flag — what web/mobile clients read."""
    rows = (await db.execute(select(FeatureFlag.key, FeatureFlag.enabled))).all()
    return {key: bool(enabled) for (key, enabled) in rows}


async def list_all(db: AsyncSession) -> list[FeatureFlag]:
    rows = (
        (await db.execute(select(FeatureFlag).order_by(FeatureFlag.key)))
        .scalars()
        .all()
    )
    return list(rows)


async def _get(db: AsyncSession, flag_id: uuid.UUID) -> FeatureFlag:
    row = (
        await db.execute(select(FeatureFlag).where(FeatureFlag.id == flag_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Flag not found"
        )
    return row


async def create(db: AsyncSession, payload: FeatureFlagCreate) -> FeatureFlag:
    row = FeatureFlag(
        key=payload.key,
        label=payload.label,
        description=payload.description,
        enabled=payload.enabled,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A flag with that key already exists.",
        ) from exc
    await db.refresh(row)
    return row


async def update(
    db: AsyncSession, flag_id: uuid.UUID, payload: FeatureFlagUpdate
) -> FeatureFlag:
    row = await _get(db, flag_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return row


async def delete(db: AsyncSession, flag_id: uuid.UUID) -> None:
    row = await _get(db, flag_id)
    await db.delete(row)
    await db.commit()


__all__ = ["create", "delete", "list_all", "public_map", "update"]
