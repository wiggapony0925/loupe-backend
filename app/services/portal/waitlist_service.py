"""Loupe Scanner waitlist — public signups + the admin pipeline.

Joining is idempotent on email: a repeat signup updates the existing row
(name/interest/quantity) instead of creating a duplicate, and re-opens a
previously ``removed`` entry. Email delivery is best-effort and never
blocks the signup.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import WaitlistStatusEnum
from app.models.waitlist import WaitlistEntry
from app.schemas.waitlist import (
    WaitlistEntryRead,
    WaitlistJoin,
    WaitlistJoined,
    WaitlistStats,
    WaitlistStatusUpdate,
)
from app.services import email_service

logger = logging.getLogger(__name__)


async def _waiting_count(db: AsyncSession) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(WaitlistEntry)
                .where(WaitlistEntry.status == WaitlistStatusEnum.waiting.value)
            )
        ).scalar_one()
    )


async def _position_of(db: AsyncSession, entry: WaitlistEntry) -> int:
    """1-based place in line among ``waiting`` signups (oldest first).

    Counts everyone ahead of this entry (older, or same instant but a
    different row) and adds one. Excluding self by id keeps this correct on
    both Postgres (exact timestamps) and SQLite (whose string timestamp
    comparison would otherwise let a row count itself).
    """
    if entry.status != WaitlistStatusEnum.waiting.value:
        return 0
    ahead = int(
        (
            await db.execute(
                select(func.count())
                .select_from(WaitlistEntry)
                .where(
                    WaitlistEntry.status == WaitlistStatusEnum.waiting.value,
                    WaitlistEntry.id != entry.id,
                    WaitlistEntry.created_at <= entry.created_at,
                )
            )
        ).scalar_one()
    )
    return ahead + 1


async def join(
    db: AsyncSession,
    payload: WaitlistJoin,
    *,
    user_id: uuid.UUID | None = None,
) -> WaitlistJoined:
    """Join (or refresh) a waitlist signup. Idempotent on email."""
    email = str(payload.email).strip().lower()
    existing = (
        await db.execute(select(WaitlistEntry).where(WaitlistEntry.email == email))
    ).scalar_one_or_none()

    if existing is None:
        entry = WaitlistEntry(
            email=email,
            name=payload.name,
            interest=payload.interest,
            referral_source=payload.referral_source,
            quantity=payload.quantity,
            user_id=user_id,
            status=WaitlistStatusEnum.waiting.value,
        )
        db.add(entry)
        is_new = True
    else:
        entry = existing
        # Refresh the details a returning visitor provided.
        if payload.name:
            entry.name = payload.name
        if payload.interest:
            entry.interest = payload.interest
        if payload.referral_source:
            entry.referral_source = payload.referral_source
        entry.quantity = payload.quantity
        if user_id is not None:
            entry.user_id = user_id
        # Re-open an opted-out signup; leave invited/purchased as-is.
        if entry.status == WaitlistStatusEnum.removed.value:
            entry.status = WaitlistStatusEnum.waiting.value
        is_new = False

    await db.commit()
    await db.refresh(entry)

    position = await _position_of(db, entry)
    if is_new:
        # Best-effort — a failed email must not fail the signup.
        try:
            await email_service.send_waitlist_confirmation(
                entry.email, name=entry.name, position=position
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("waitlist confirmation email failed: %s", exc)

    return WaitlistJoined(
        id=entry.id,
        email=entry.email,
        status=WaitlistStatusEnum(entry.status),
        position=position,
        created_at=entry.created_at,
    )


async def stats(db: AsyncSession) -> WaitlistStats:
    total = int(
        (await db.execute(select(func.count()).select_from(WaitlistEntry))).scalar_one()
    )
    return WaitlistStats(total=total, waiting=await _waiting_count(db))


async def admin_list(
    db: AsyncSession, *, status_filter: str | None = None
) -> list[WaitlistEntryRead]:
    stmt = select(WaitlistEntry).order_by(WaitlistEntry.created_at.desc())
    if status_filter:
        stmt = stmt.where(WaitlistEntry.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return [WaitlistEntryRead.model_validate(r) for r in rows]


async def admin_update_status(
    db: AsyncSession, entry_id: uuid.UUID, payload: WaitlistStatusUpdate
) -> WaitlistEntryRead:
    entry = (
        await db.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_id))
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Waitlist entry not found"
        )
    previous_status = entry.status
    entry.status = payload.status.value
    await db.commit()
    await db.refresh(entry)
    # The confirmation email promises "we'll email you when your spot opens
    # up" — advancing to `invited` is that moment. Transition-only (re-saving
    # `invited` stays silent) and best-effort, after the commit.
    if (
        entry.status == WaitlistStatusEnum.invited.value
        and previous_status != WaitlistStatusEnum.invited.value
    ):
        await email_service.send_waitlist_invite(entry.email, name=entry.name)
    return WaitlistEntryRead.model_validate(entry)


async def admin_delete(db: AsyncSession, entry_id: uuid.UUID) -> None:
    entry = (
        await db.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_id))
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Waitlist entry not found"
        )
    await db.delete(entry)
    await db.commit()


__all__ = [
    "admin_delete",
    "admin_list",
    "admin_update_status",
    "join",
    "stats",
]
