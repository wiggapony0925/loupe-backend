"""Admin user management (`/v1/admin/users`).

List/search users, grant or revoke admin, ban/unban, and soft-delete.
Super-admins (``ADMIN_EMAILS``) and the caller's own account are protected.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.schemas.admin import AdminRoleUpdate, AdminUserPage, AdminUserRead, BanRequest
from app.services import audit_service
from app.services.admin import user_admin_service

router = APIRouter(prefix="/users", tags=["admin-users"])


@router.get("", response_model=AdminUserPage, summary="List / search users")
async def list_users(
    q: str | None = Query(None, max_length=120, description="Search email or name."),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AdminUserPage:
    return await user_admin_service.list_users(db, q=q, page=page, page_size=page_size)


@router.patch(
    "/{user_id}/role", response_model=AdminUserRead, summary="Grant/revoke admin"
)
async def set_role(
    user_id: uuid.UUID,
    payload: AdminRoleUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
) -> AdminUserRead:
    result = await user_admin_service.set_admin(db, actor, user_id, payload.is_admin)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="user.role",
        target_table="users",
        target_id=user_id,
        payload={"is_admin": payload.is_admin},
    )
    return result


@router.post("/{user_id}/ban", response_model=AdminUserRead, summary="Ban a user")
async def ban_user(
    user_id: uuid.UUID,
    payload: BanRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
) -> AdminUserRead:
    result = await user_admin_service.ban(db, actor, user_id, payload.reason)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="user.ban",
        target_table="users",
        target_id=user_id,
        payload={"reason": payload.reason},
    )
    return result


@router.post("/{user_id}/unban", response_model=AdminUserRead, summary="Unban a user")
async def unban_user(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
) -> AdminUserRead:
    result = await user_admin_service.unban(db, user_id)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="user.unban",
        target_table="users",
        target_id=user_id,
    )
    return result


@router.delete(
    "/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a user (soft)"
)
async def delete_user(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
) -> None:
    await user_admin_service.soft_delete(db, actor, user_id)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="user.delete",
        target_table="users",
        target_id=user_id,
    )


__all__ = ["router"]
