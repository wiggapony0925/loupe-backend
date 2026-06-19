"""At-a-glance metrics for the admin portal overview."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.blog import BlogPost
from app.models.career import JobApplication, JobPosting
from app.models.enums import BlogStatusEnum, JobStatusEnum, WaitlistStatusEnum
from app.models.user import User
from app.models.waitlist import WaitlistEntry
from app.schemas.admin import AdminMetrics


async def _count(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar_one())


async def summary(db: AsyncSession) -> AdminMetrics:
    now = datetime.now(UTC)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    users = select(func.count()).select_from(User).where(User.deleted_at.is_(None))
    return AdminMetrics(
        users_total=await _count(db, users),
        users_new_7d=await _count(db, users.where(User.created_at >= d7)),
        users_new_30d=await _count(db, users.where(User.created_at >= d30)),
        admins=await _count(db, users.where(User.is_admin.is_(True))),
        banned=await _count(
            db,
            select(func.count()).select_from(User).where(User.banned_at.is_not(None)),
        ),
        jobs_total=await _count(db, select(func.count()).select_from(JobPosting)),
        jobs_open=await _count(
            db,
            select(func.count())
            .select_from(JobPosting)
            .where(JobPosting.status == JobStatusEnum.open.value),
        ),
        applications_total=await _count(
            db, select(func.count()).select_from(JobApplication)
        ),
        applications_new_7d=await _count(
            db,
            select(func.count())
            .select_from(JobApplication)
            .where(JobApplication.created_at >= d7),
        ),
        posts_total=await _count(db, select(func.count()).select_from(BlogPost)),
        posts_published=await _count(
            db,
            select(func.count())
            .select_from(BlogPost)
            .where(BlogPost.status == BlogStatusEnum.published.value),
        ),
        waitlist_total=await _count(
            db, select(func.count()).select_from(WaitlistEntry)
        ),
        waitlist_waiting=await _count(
            db,
            select(func.count())
            .select_from(WaitlistEntry)
            .where(WaitlistEntry.status == WaitlistStatusEnum.waiting.value),
        ),
    )


__all__ = ["summary"]
