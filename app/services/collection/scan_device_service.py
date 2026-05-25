"""Scanner CRUD + heartbeat tracking."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scanner import Scanner
from app.models.user import User
from app.schemas.scanner import ScannerCreate, ScannerHeartbeat, ScannerUpdate
from app.utils.time import utcnow


async def list_for_user(db: AsyncSession, user: User) -> list[Scanner]:
    rows = await db.execute(
        select(Scanner)
        .where(Scanner.owner_id == user.id)
        .order_by(Scanner.created_at.desc())
    )
    return list(rows.scalars().all())


async def get_for_user(
    db: AsyncSession, user: User, scanner_id: uuid.UUID
) -> Scanner | None:
    return (
        await db.execute(
            select(Scanner).where(Scanner.id == scanner_id, Scanner.owner_id == user.id)
        )
    ).scalar_one_or_none()


async def pair(db: AsyncSession, user: User, payload: ScannerCreate) -> Scanner:
    """Register a new scanner for the user, or return the existing pairing."""
    existing = (
        await db.execute(
            select(Scanner).where(
                Scanner.device_id == payload.device_id, Scanner.owner_id == user.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.name = payload.name or existing.name
        existing.firmware_version = (
            payload.firmware_version or existing.firmware_version
        )
        existing.transport = payload.transport
        existing.is_active = True
        await db.commit()
        await db.refresh(existing)
        return existing
    scanner = Scanner(
        owner_id=user.id,
        device_id=payload.device_id,
        name=payload.name,
        firmware_version=payload.firmware_version,
        transport=payload.transport,
        is_active=True,
    )
    db.add(scanner)
    await db.commit()
    await db.refresh(scanner)
    return scanner


async def update(
    db: AsyncSession, user: User, scanner_id: uuid.UUID, patch: ScannerUpdate
) -> Scanner | None:
    scanner = await get_for_user(db, user, scanner_id)
    if scanner is None:
        return None
    if patch.name is not None:
        scanner.name = patch.name
    if patch.firmware_version is not None:
        scanner.firmware_version = patch.firmware_version
    if patch.transport is not None:
        scanner.transport = patch.transport
    if patch.is_active is not None:
        scanner.is_active = patch.is_active
    await db.commit()
    await db.refresh(scanner)
    return scanner


async def delete(db: AsyncSession, user: User, scanner_id: uuid.UUID) -> bool:
    scanner = await get_for_user(db, user, scanner_id)
    if scanner is None:
        return False
    await db.delete(scanner)
    await db.commit()
    return True


async def record_heartbeat(
    db: AsyncSession, user: User, scanner_id: uuid.UUID, payload: ScannerHeartbeat
) -> Scanner | None:
    scanner = await get_for_user(db, user, scanner_id)
    if scanner is None:
        return None
    scanner.last_seen_at = utcnow()
    if payload.firmware_version is not None:
        scanner.firmware_version = payload.firmware_version
    await db.commit()
    await db.refresh(scanner)
    return scanner


__all__ = [
    "delete",
    "get_for_user",
    "list_for_user",
    "pair",
    "record_heartbeat",
    "update",
]
