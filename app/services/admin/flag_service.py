"""Feature-flag CRUD + the public flag map clients gate on."""

from __future__ import annotations

import re
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feature_flag import FeatureFlag
from app.schemas.flag import FeatureFlagCreate, FeatureFlagUpdate, FeatureFlagUpsert

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")


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


async def upsert_by_key(
    db: AsyncSession, key: str, payload: FeatureFlagUpsert
) -> FeatureFlag:
    """Set a flag's enabled state by key, creating it if it doesn't exist.

    Powers the in-app inspect overlay (toggle a component's flag without an id).
    """
    key = key.strip().lower()
    if not _KEY_RE.match(key):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="key must be lowercase letters, digits, and underscores (start with a letter)",
        )
    row = (
        await db.execute(select(FeatureFlag).where(FeatureFlag.key == key))
    ).scalar_one_or_none()
    if row is None:
        row = FeatureFlag(
            key=key,
            label=payload.label or key,
            description=payload.description,
            enabled=payload.enabled,
        )
        db.add(row)
    else:
        row.enabled = payload.enabled
        if payload.label:
            row.label = payload.label
        if payload.description is not None:
            row.description = payload.description
    await db.commit()
    await db.refresh(row)
    return row


__all__ = ["create", "delete", "list_all", "public_map", "update", "upsert_by_key"]
