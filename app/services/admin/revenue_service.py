"""Revenue analytics derived from Loupe Pro subscription state on ``User``.

All read-only. Subscriber buckets are mutually exclusive:
  paying   — plan=pro with an active Stripe subscription, not trialing
  trialing — in a Stripe free trial
  comped   — plan=pro with no Stripe subscription (admin grant / lifetime)
  free     — everyone else
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.user import User
from app.schemas.revenue import RevenueMonthPoint, RevenueSummary
from app.services import billing_service

_TREND_MONTHS = 6


async def _count(db: AsyncSession, *conditions) -> int:
    stmt = select(func.count()).select_from(User).where(User.deleted_at.is_(None))
    for c in conditions:
        stmt = stmt.where(c)
    return int((await db.execute(stmt)).scalar_one())


async def summary(db: AsyncSession) -> RevenueSummary:
    settings = get_settings()
    now = datetime.now(UTC)
    d30 = now - timedelta(days=30)

    paying_cond = and_(
        User.plan == "pro",
        User.stripe_subscription_id.is_not(None),
        User.pro_trialing.is_(False),
    )
    comped_cond = and_(User.plan == "pro", User.stripe_subscription_id.is_(None))

    paying = await _count(db, paying_cond)
    trialing = await _count(db, User.pro_trialing.is_(True))
    comped = await _count(db, comped_cond)
    total_users = await _count(db)
    free = max(total_users - paying - trialing - comped, 0)

    new_pro_30d = await _count(db, User.pro_since.is_not(None), User.pro_since >= d30)
    churned_30d = await _count(
        db,
        User.plan == "free",
        User.pro_since.is_not(None),
        User.pro_expires_at.is_not(None),
        User.pro_expires_at >= d30,
        User.pro_expires_at <= now,
    )
    churn_base = paying + churned_30d
    churn_rate = (churned_30d / churn_base) if churn_base else 0.0

    est_mrr = paying * settings.pro_price_monthly_usd

    return RevenueSummary(
        billing_configured=billing_service.billing_configured(),
        paying=paying,
        trialing=trialing,
        comped=comped,
        free=free,
        total_users=total_users,
        price_monthly_usd=settings.pro_price_monthly_usd,
        price_yearly_usd=settings.pro_price_yearly_usd,
        est_mrr_usd=round(est_mrr, 2),
        est_arr_usd=round(est_mrr * 12, 2),
        new_pro_30d=new_pro_30d,
        churned_30d=churned_30d,
        churn_rate_30d=round(churn_rate, 4),
        pro_by_month=await _pro_by_month(db, now),
    )


async def _pro_by_month(db: AsyncSession, now: datetime) -> list[RevenueMonthPoint]:
    """New-Pro counts for the last N months. Bucketed in Python so the query
    stays portable across Postgres and the SQLite test database."""
    since = (now.replace(day=1) - timedelta(days=31 * (_TREND_MONTHS - 1))).replace(
        day=1
    )
    rows = (
        await db.execute(
            select(User.pro_since).where(
                User.deleted_at.is_(None),
                User.pro_since.is_not(None),
                User.pro_since >= since,
            )
        )
    ).scalars().all()
    counts: Counter[str] = Counter(d.strftime("%Y-%m") for d in rows if d)

    # Emit a continuous run of months (zero-filled) ending with the current one.
    months: list[str] = []
    cursor = now.replace(day=1)
    for _ in range(_TREND_MONTHS):
        months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    months.reverse()
    return [RevenueMonthPoint(month=m, new_pro=counts.get(m, 0)) for m in months]


__all__ = ["summary"]
