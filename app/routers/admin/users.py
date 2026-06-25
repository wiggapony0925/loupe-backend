"""Admin user management (`/v1/admin/users`).

List/search users, grant or revoke admin, ban/unban, and soft-delete.
Super-admins (``ADMIN_EMAILS``) and the caller's own account are protected.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin, require_super_admin
from app.db import get_db
from app.models.user import User
from app.schemas.admin import (
    AdminPlanUpdate,
    AdminRoleUpdate,
    AdminUserDetail,
    AdminUserPage,
    AdminUserRead,
    BanRequest,
    ImpersonateResult,
    RefundResult,
    SubscriptionCancelRequest,
    TestAccountCreated,
)
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


# NOTE: literal `/test` is declared before `/{user_id}` so it isn't shadowed.
@router.post(
    "/test",
    response_model=TestAccountCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a sandbox test account",
)
async def create_test_account(
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
) -> TestAccountCreated:
    result = await user_admin_service.create_test_account(db)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="user.test_account",
        target_table="users",
        target_id=result.id,
        payload={"email": result.email},
    )
    return result


@router.get(
    "/{user_id}", response_model=AdminUserDetail, summary="Get a user's full detail"
)
async def get_user(
    user_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> AdminUserDetail:
    return await user_admin_service.get_detail(db, user_id)


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


@router.patch(
    "/{user_id}/plan",
    response_model=AdminUserRead,
    summary="Comp a user to Loupe Pro (or back to free)",
)
async def set_plan(
    user_id: uuid.UUID,
    payload: AdminPlanUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
) -> AdminUserRead:
    result = await user_admin_service.set_plan(db, user_id, payload.plan)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="user.plan",
        target_table="users",
        target_id=user_id,
        payload={"plan": payload.plan},
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


@router.post(
    "/{user_id}/revoke-sessions",
    response_model=AdminUserDetail,
    summary="Sign a user out of every device (revoke all tokens)",
)
async def revoke_sessions(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
) -> AdminUserDetail:
    result = await user_admin_service.revoke_sessions(db, user_id)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="user.revoke_sessions",
        target_table="users",
        target_id=user_id,
    )
    return result


@router.post(
    "/{user_id}/subscription/cancel",
    response_model=AdminUserDetail,
    summary="Cancel a user's Stripe subscription (super-admin)",
)
async def cancel_subscription(
    user_id: uuid.UUID,
    payload: SubscriptionCancelRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
) -> AdminUserDetail:
    result = await user_admin_service.cancel_subscription(
        db, user_id, immediately=payload.immediately
    )
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="user.subscription_cancel",
        target_table="users",
        target_id=user_id,
        payload={"immediately": payload.immediately},
    )
    return result


@router.post(
    "/{user_id}/refund",
    response_model=RefundResult,
    summary="Refund a user's latest charge (super-admin)",
)
async def refund_latest(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
) -> RefundResult:
    result = await user_admin_service.refund_latest(db, user_id)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="user.refund",
        target_table="users",
        target_id=user_id,
        payload={
            "amount_usd": result.get("amount_usd"),
            "refund_id": result.get("refund_id"),
        },
    )
    return RefundResult(**result)


@router.post(
    "/{user_id}/impersonate",
    response_model=ImpersonateResult,
    summary="Get a short-lived token to view the app as a user (super-admin)",
)
async def impersonate(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
) -> ImpersonateResult:
    result = await user_admin_service.impersonate(db, actor, user_id)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="user.impersonate",
        target_table="users",
        target_id=user_id,
        payload={"email": result["email"]},
    )
    return ImpersonateResult(**result)


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
