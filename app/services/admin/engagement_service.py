"""Engagement / retention analytics. All read-only.

Activity is inferred from the two first-class collector actions that carry a
timestamp — scanning a card (``scan_jobs``) and adding one (``graded_cards``) —
since there's no dedicated sessions/events table.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.grade import GradedCard
from app.models.scan import ScanJob
from app.models.user import User
from app.schemas.engagement import EngagementSummary, FunnelStep, WeekPoint

_TREND_WEEKS = 8


async def _count(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar_one())


async def _active_user_ids(db: AsyncSession, since: datetime) -> set:
    """Distinct users who scanned or added a card since ``since``."""
    scanned = (
        await db.execute(
            select(ScanJob.user_id.distinct()).where(ScanJob.created_at >= since)
        )
    ).scalars().all()
    added = (
        await db.execute(
            select(GradedCard.user_id.distinct()).where(
                GradedCard.created_at >= since, GradedCard.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    return {u for u in scanned if u} | {u for u in added if u}


async def summary(db: AsyncSession) -> EngagementSummary:
    now = datetime.now(UTC)
    users = select(func.count()).select_from(User).where(User.deleted_at.is_(None))

    total_users = await _count(db, users)
    pro_users = await _count(db, users.where(User.plan == "pro"))

    # Distinct users who have ever added a card = "activated".
    activated = await _count(
        db,
        select(func.count(func.distinct(GradedCard.user_id))).where(
            GradedCard.deleted_at.is_(None)
        ),
    )

    active_7d = len(await _active_user_ids(db, now - timedelta(days=7)))
    active_30d = len(await _active_user_ids(db, now - timedelta(days=30)))
    active_90d = len(await _active_user_ids(db, now - timedelta(days=90)))

    return EngagementSummary(
        total_users=total_users,
        active_7d=active_7d,
        active_30d=active_30d,
        active_90d=active_90d,
        activated_users=activated,
        activation_rate=round(activated / total_users, 4) if total_users else 0.0,
        pro_users=pro_users,
        pro_rate=round(pro_users / total_users, 4) if total_users else 0.0,
        new_users_by_week=await _new_users_by_week(db, now),
        funnel=[
            FunnelStep(label="Signed up", count=total_users),
            FunnelStep(label="Added a card", count=activated),
            FunnelStep(label="Upgraded to Pro", count=pro_users),
        ],
    )


async def _new_users_by_week(db: AsyncSession, now: datetime) -> list[WeekPoint]:
    """New-user counts per ISO week for the last N weeks (zero-filled)."""
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    since = monday - timedelta(weeks=_TREND_WEEKS - 1)
    rows = (
        await db.execute(
            select(User.created_at).where(
                User.deleted_at.is_(None), User.created_at >= since
            )
        )
    ).scalars().all()

    def week_key(d: datetime) -> str:
        start = (d - timedelta(days=d.weekday())).date()
        return start.isoformat()

    counts: Counter[str] = Counter(week_key(d) for d in rows if d)
    weeks = [(monday - timedelta(weeks=i)).date().isoformat() for i in range(_TREND_WEEKS)]
    weeks.reverse()
    return [WeekPoint(week=w, new_users=counts.get(w, 0)) for w in weeks]


__all__ = ["summary"]
