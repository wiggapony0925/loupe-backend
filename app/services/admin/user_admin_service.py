"""User management for the admin portal — list/search, grant admin, ban, delete.

Safety rails: a super-admin (an email in ``ADMIN_EMAILS``) can never be
demoted, banned, or deleted, and an admin can't ban/demote/delete their own
account — so there's always a way back in.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import is_super_admin
from app.models.grade import GradedCard
from app.models.scan import ScanJob
from app.models.user import User
from app.models.watchlist import WatchlistItem
from app.schemas.admin import (
    AdminUserDetail,
    AdminUserPage,
    AdminUserRead,
    TestAccountCreated,
)
from app.services import billing_service, email_service
from app.services.auth import user_service


def _auth_method(user: User) -> str:
    if user.apple_subject:
        return "apple"
    if user.google_subject:
        return "google"
    if user.password_hash:
        return "password"
    return "unknown"


def to_read(user: User) -> AdminUserRead:
    out = AdminUserRead.model_validate(user)
    out.is_super_admin = is_super_admin(user)
    out.banned = user.banned_at is not None
    out.deleted = user.deleted_at is not None
    out.has_subscription = bool(user.stripe_subscription_id)
    return out


async def get_detail(db: AsyncSession, user_id: uuid.UUID) -> AdminUserDetail:
    """A user's full record + activity aggregates (vault, watchlist, scans)."""
    user = await _get(db, user_id)

    async def count(model) -> int:
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.user_id == user_id)
                )
            ).scalar_one()
        )

    est = (
        await db.execute(
            select(func.coalesce(func.sum(GradedCard.estimated_value_usd), 0)).where(
                GradedCard.user_id == user_id
            )
        )
    ).scalar_one()

    out = AdminUserDetail.model_validate(user)
    out.is_super_admin = is_super_admin(user)
    out.banned = user.banned_at is not None
    out.deleted = user.deleted_at is not None
    out.has_subscription = bool(user.stripe_subscription_id)
    out.auth_method = _auth_method(user)
    out.grades_count = await count(GradedCard)
    out.watchlist_count = await count(WatchlistItem)
    out.scans_count = await count(ScanJob)
    out.estimated_value_usd = float(est or 0)
    return out


async def create_test_account(db: AsyncSession) -> TestAccountCreated:
    """Mint a sandbox account with a random password (returned once)."""
    email = f"test+{secrets.token_hex(4)}@loupe.app"
    password = secrets.token_urlsafe(12)
    user = await user_service.create_with_password(
        db, email=email, password=password, display_name="Test account"
    )
    return TestAccountCreated(id=user.id, email=email, password=password)


async def list_users(
    db: AsyncSession, *, q: str | None = None, page: int = 1, page_size: int = 25
) -> AdminUserPage:
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    where = []
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        where.append(
            or_(
                func.lower(User.email).like(like),
                func.lower(User.display_name).like(like),
            )
        )

    total = (
        await db.execute(select(func.count()).select_from(User).where(*where))
    ).scalar_one()
    rows = (
        (
            await db.execute(
                select(User)
                .where(*where)
                .order_by(User.created_at.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        )
        .scalars()
        .all()
    )
    return AdminUserPage(
        results=[to_read(u) for u in rows],
        total=int(total),
        page=page,
        page_size=page_size,
    )


async def _get(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return user


def _guard_not_self(actor: User, target: User) -> None:
    if actor.id == target.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You can't perform this action on your own account.",
        )


def _guard_not_super(target: User) -> None:
    if is_super_admin(target):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is a protected super-admin.",
        )


async def set_admin(
    db: AsyncSession, actor: User, user_id: uuid.UUID, is_admin: bool
) -> AdminUserRead:
    target = await _get(db, user_id)
    _guard_not_super(target)  # super-admins are always admin; not toggleable
    _guard_not_self(actor, target)
    target.is_admin = is_admin
    await db.commit()
    await db.refresh(target)
    if is_admin:
        await email_service.send_admin_granted(target)
    return to_read(target)


async def set_plan(db: AsyncSession, user_id: uuid.UUID, plan: str) -> AdminUserRead:
    """Comp a user to Loupe Pro (or revoke). A no-expiry grant — handy for
    testers and support before Stripe is wired."""
    target = await _get(db, user_id)
    target.plan = plan
    if plan == "pro":
        if target.pro_since is None:
            target.pro_since = datetime.now(UTC)
        target.pro_expires_at = None  # comp = lifetime until changed
    else:
        target.pro_expires_at = None
    await db.commit()
    await db.refresh(target)
    return to_read(target)


async def ban(
    db: AsyncSession, actor: User, user_id: uuid.UUID, reason: str | None
) -> AdminUserRead:
    target = await _get(db, user_id)
    _guard_not_super(target)
    _guard_not_self(actor, target)
    target.banned_at = datetime.now(UTC)
    target.ban_reason = (reason or "").strip() or None
    await db.commit()
    await db.refresh(target)
    await email_service.send_ban_notice(target, target.ban_reason)
    return to_read(target)


async def unban(db: AsyncSession, user_id: uuid.UUID) -> AdminUserRead:
    target = await _get(db, user_id)
    target.banned_at = None
    target.ban_reason = None
    await db.commit()
    await db.refresh(target)
    return to_read(target)


async def soft_delete(db: AsyncSession, actor: User, user_id: uuid.UUID) -> None:
    target = await _get(db, user_id)
    _guard_not_super(target)
    _guard_not_self(actor, target)
    target.deleted_at = datetime.now(UTC)
    await db.commit()


async def revoke_sessions(db: AsyncSession, user_id: uuid.UUID) -> AdminUserDetail:
    """Sign a user out everywhere by bumping their token epoch.

    Every outstanding access/refresh token carries the ``token_version`` current
    at sign-in, so incrementing it invalidates all of them at once (the support
    "revoke a stolen/compromised session" action). Stateless — no token store.
    """
    target = await _get(db, user_id)
    target.token_version += 1
    await db.commit()
    return await get_detail(db, user_id)


async def cancel_subscription(
    db: AsyncSession, user_id: uuid.UUID, *, immediately: bool = False
) -> AdminUserDetail:
    """Cancel a user's Stripe subscription on their behalf (support action).

    Delegates to Stripe via :mod:`billing_service`; the webhook reconciles the
    plan. Raises 400 if the user has no subscription, 503 if billing is off.
    """
    target = await _get(db, user_id)
    await billing_service.cancel_subscription(db, target, immediately=immediately)
    return await get_detail(db, user_id)


async def refund_latest(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """Refund a user's most recent charge (super-admin support action).

    Raises 400 if there's no billing account / refundable charge, 503 if billing
    is off. Returns the Stripe refund result.
    """
    target = await _get(db, user_id)
    return await billing_service.refund_latest(db, target)


__all__ = [
    "ban",
    "cancel_subscription",
    "create_test_account",
    "get_detail",
    "list_users",
    "refund_latest",
    "revoke_sessions",
    "set_admin",
    "soft_delete",
    "to_read",
    "unban",
]
