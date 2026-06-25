"""Live activity feed — recent signups, scans, acquisitions, and admin actions
merged into one time-ordered stream. Read-only.

Each source is queried for its newest ``limit`` rows; merging and re-slicing to
``limit`` then yields the correct global most-recent set (the global top-N is
always a subset of each source's top-N).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.card import Card
from app.models.grade import GradedCard
from app.models.scan import ScanJob
from app.models.user import User
from app.schemas.pulse import PulseEvent, PulseFeed

_DEFAULT_LIMIT = 40


def _enum(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


async def recent(db: AsyncSession, *, limit: int = _DEFAULT_LIMIT) -> PulseFeed:
    limit = max(1, min(limit, 100))
    events: list[PulseEvent] = []

    # ── Sign-ups ──
    rows = (
        await db.execute(
            select(User.id, User.email, User.display_name, User.created_at)
            .where(User.deleted_at.is_(None))
            .order_by(User.created_at.desc())
            .limit(limit)
        )
    ).all()
    for uid, email, name, at in rows:
        events.append(
            PulseEvent(
                id=f"signup:{uid}",
                type="signup",
                at=at,
                actor=name or email,
                title="New sign-up",
            )
        )

    # ── Scans ──
    rows = (
        await db.execute(
            select(
                ScanJob.id,
                ScanJob.status,
                User.email,
                User.display_name,
                ScanJob.created_at,
            )
            .join(User, ScanJob.user_id == User.id)
            .order_by(ScanJob.created_at.desc())
            .limit(limit)
        )
    ).all()
    for sid, status, email, name, at in rows:
        events.append(
            PulseEvent(
                id=f"scan:{sid}",
                type="scan",
                at=at,
                actor=name or email,
                title="Scanned a card",
                detail=_enum(status),
            )
        )

    # ── Acquisitions (added to a collection) ──
    rows = (
        await db.execute(
            select(
                GradedCard.id,
                Card.name,
                GradedCard.grade,
                GradedCard.house,
                GradedCard.estimated_value_usd,
                User.email,
                User.display_name,
                GradedCard.created_at,
            )
            .join(User, GradedCard.user_id == User.id)
            .join(Card, GradedCard.card_id == Card.id)
            .where(GradedCard.deleted_at.is_(None))
            .order_by(GradedCard.created_at.desc())
            .limit(limit)
        )
    ).all()
    for gid, card_name, grade, house, est, email, name, at in rows:
        events.append(
            PulseEvent(
                id=f"acquisition:{gid}",
                type="acquisition",
                at=at,
                actor=name or email,
                title=f"Added {card_name}",
                detail=f"{_enum(house).upper()} {grade}",
                value_usd=float(est) if est is not None else None,
            )
        )

    # ── Admin actions ──
    rows = (
        await db.execute(
            select(
                AuditLog.id,
                AuditLog.action,
                AuditLog.target_table,
                User.email,
                AuditLog.created_at,
            )
            .outerjoin(User, AuditLog.user_id == User.id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    for aid, action, target, email, at in rows:
        events.append(
            PulseEvent(
                id=f"admin:{aid}",
                type="admin",
                at=at,
                actor=email,
                title=action,
                detail=target,
            )
        )

    events.sort(key=lambda e: e.at, reverse=True)
    return PulseFeed(events=events[:limit])


__all__ = ["recent"]
