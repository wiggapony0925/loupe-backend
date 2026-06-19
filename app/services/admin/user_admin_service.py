"""User management for the admin portal — list/search, grant admin, ban, delete.

Safety rails: a super-admin (an email in ``ADMIN_EMAILS``) can never be
demoted, banned, or deleted, and an admin can't ban/demote/delete their own
account — so there's always a way back in.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import is_super_admin
from app.models.user import User
from app.schemas.admin import AdminUserPage, AdminUserRead


def to_read(user: User) -> AdminUserRead:
    out = AdminUserRead.model_validate(user)
    out.is_super_admin = is_super_admin(user)
    out.banned = user.banned_at is not None
    out.deleted = user.deleted_at is not None
    return out


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


__all__ = ["ban", "list_users", "set_admin", "soft_delete", "to_read", "unban"]
