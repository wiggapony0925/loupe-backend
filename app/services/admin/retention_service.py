"""Cohort-retention triangle. Read-only.

Groups users by signup week, then for each week-offset N measures the share of
the cohort that was *active* (scanned or added a card) in week N. Computed in
Python so the query stays portable across Postgres and the SQLite test DB.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.grade import GradedCard
from app.models.scan import ScanJob
from app.models.user import User
from app.schemas.retention import CohortRow, RetentionReport

_WEEKS = 8


def _monday(d: datetime | date) -> date:
    dd = d.date() if isinstance(d, datetime) else d
    return dd - timedelta(days=dd.weekday())


async def cohorts(db: AsyncSession, *, weeks: int = _WEEKS) -> RetentionReport:
    now = datetime.now(UTC)
    this_monday = _monday(now)
    since = this_monday - timedelta(weeks=weeks - 1)

    # Signup week per user.
    users = (
        await db.execute(
            select(User.id, User.created_at).where(
                User.deleted_at.is_(None), User.created_at >= since
            )
        )
    ).all()
    cohort_of: dict = {}
    cohort_size: dict[date, int] = {}
    for uid, created in users:
        cw = _monday(created)
        cohort_of[uid] = cw
        cohort_size[cw] = cohort_size.get(cw, 0) + 1

    # Activity weeks per user (scans + card additions).
    activity: dict = {}  # user_id -> set[week_monday]

    async def _collect(stmt) -> None:
        for uid, ts in (await db.execute(stmt)).all():
            if uid is None or ts is None:
                continue
            activity.setdefault(uid, set()).add(_monday(ts))

    await _collect(
        select(ScanJob.user_id, ScanJob.created_at).where(ScanJob.created_at >= since)
    )
    await _collect(
        select(GradedCard.user_id, GradedCard.created_at).where(
            GradedCard.created_at >= since, GradedCard.deleted_at.is_(None)
        )
    )

    # active[cohort][offset] = count of distinct users active in that week.
    active: dict[date, dict[int, int]] = {cw: {} for cw in cohort_size}
    for uid, cw in cohort_of.items():
        for aw in activity.get(uid, ()):
            offset = (aw - cw).days // 7
            if offset < 0:
                continue
            active[cw][offset] = active[cw].get(offset, 0) + 1

    rows: list[CohortRow] = []
    cw = since
    while cw <= this_monday:
        size = cohort_size.get(cw, 0)
        # Triangle: only weeks that have elapsed for this cohort.
        elapsed = (this_monday - cw).days // 7
        retention = [
            round((active.get(cw, {}).get(n, 0) / size), 4) if size else 0.0
            for n in range(elapsed + 1)
        ]
        rows.append(CohortRow(cohort=cw.isoformat(), size=size, retention=retention))
        cw += timedelta(weeks=1)

    return RetentionReport(weeks=weeks, cohorts=rows)


__all__ = ["cohorts"]
