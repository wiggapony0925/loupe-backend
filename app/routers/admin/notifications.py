"""Admin notification composer (`/v1/admin/notifications`).

The push equivalent of the email announcement composer: write a notification,
see exactly how many people it will reach, send a test to yourself, then send
it for real. Everything is audit-logged, because a broadcast reaches every
phone at once and "who sent that" needs an answer.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.notification import CATEGORIES, Notification
from app.models.push_token import PushToken
from app.models.user import User, UserSettings
from app.schemas.notification import (
    AdminNotificationCreate,
    AdminNotificationResult,
    NotificationPage,
    NotificationRead,
)
from app.services import audit_service, notification_service

router = APIRouter(prefix="/notifications", tags=["admin-notifications"])


class _Audience:
    """How many people a send would actually reach."""

    def __init__(self, users: int, devices: int, push_enabled: int) -> None:
        self.users = users
        self.devices = devices
        self.push_enabled = push_enabled


async def _audience(db: AsyncSession) -> _Audience:
    users = int(
        (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(User.deleted_at.is_(None), User.banned_at.is_(None))
            )
        ).scalar_one()
    )
    devices = int(
        (await db.execute(select(func.count()).select_from(PushToken))).scalar_one()
    )
    push_enabled = int(
        (
            await db.execute(
                select(func.count())
                .select_from(User)
                .outerjoin(UserSettings, UserSettings.user_id == User.id)
                .where(
                    User.deleted_at.is_(None),
                    User.banned_at.is_(None),
                    (UserSettings.user_id.is_(None))
                    | (UserSettings.push_notifications_enabled.is_(True)),
                )
            )
        ).scalar_one()
    )
    return _Audience(users, devices, push_enabled)


@router.get("/audience", summary="Who a broadcast would reach")
async def get_audience(db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    """Real counts, so the composer can say '312 users · 87 devices' rather
    than sending blind. `devices` is registered push tokens — the gap between
    it and `push_enabled` is users who simply never installed the app."""
    a = await _audience(db)
    return {"users": a.users, "devices": a.devices, "push_enabled": a.push_enabled}


@router.post(
    "",
    response_model=AdminNotificationResult,
    summary="Send a notification (one user or broadcast)",
)
async def send_notification(
    payload: AdminNotificationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminNotificationResult:
    if payload.category not in CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"category must be one of: {', '.join(sorted(CATEGORIES))}",
        )
    audience = await _audience(db)
    reach = 1 if payload.user_id else audience.users

    if payload.dry_run:
        # Nothing written, nothing pushed — the composer's "preview" button.
        return AdminNotificationResult(created=0, audience=reach, dry_run=True)

    if payload.user_id is not None:
        target = await db.get(User, payload.user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
        row = await notification_service.notify(
            db,
            target.id,
            category=payload.category,
            kind="admin_direct",
            title=payload.title,
            body=payload.body,
            href=payload.href,
            image_url=payload.image_url,
            push=payload.push,
        )
        created = 1 if row is not None else 0
    else:
        created = await notification_service.broadcast(
            db,
            category=payload.category,
            kind="admin_broadcast",
            title=payload.title,
            body=payload.body,
            href=payload.href,
            image_url=payload.image_url,
            # Admin copy is written fresh each time — two identical
            # announcements a month apart are both legitimate, so no dedupe.
            push=payload.push,
        )

    await audit_service.record(
        db,
        request=request,
        user=admin,
        action="notification.send",
        payload={
            "title": payload.title,
            "category": payload.category,
            "target": str(payload.user_id) if payload.user_id else "broadcast",
            "created": created,
            "push": payload.push,
        },
    )
    return AdminNotificationResult(created=created, audience=reach, dry_run=False)


@router.post(
    "/test",
    response_model=AdminNotificationResult,
    summary="Send the composed notification to yourself only",
)
async def send_test(
    payload: AdminNotificationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminNotificationResult:
    """The safety rail before a broadcast: see it on your own phone first."""
    row = await notification_service.notify(
        db,
        admin.id,
        category=payload.category if payload.category in CATEGORIES else "system",
        kind="admin_test",
        title=payload.title,
        body=payload.body,
        href=payload.href,
        image_url=payload.image_url,
        push=payload.push,
    )
    await audit_service.record(
        db,
        request=request,
        user=admin,
        action="notification.test_send",
        payload={"title": payload.title},
    )
    return AdminNotificationResult(created=1 if row else 0, audience=1, dry_run=False)


@router.get(
    "/log",
    response_model=NotificationPage,
    summary="Recently sent notifications",
)
async def list_log(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> NotificationPage:
    """What actually went out, newest first. Scoped to one user when asked —
    the "why didn't they get it?" view."""
    where = []
    if user_id is not None:
        where.append(Notification.user_id == user_id)
    total = int(
        (
            await db.execute(
                select(func.count()).select_from(Notification).where(*where)
            )
        ).scalar_one()
    )
    # Unread across the whole filtered log, not just this page — "was the
    # broadcast opened?" is the question an operator is actually asking.
    unread = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Notification)
                .where(*where, Notification.read_at.is_(None))
            )
        ).scalar_one()
    )
    rows = (
        (
            await db.execute(
                select(Notification)
                .where(*where)
                .order_by(Notification.created_at.desc(), Notification.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return NotificationPage(
        items=[NotificationRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        unread=unread,
    )


__all__ = ["router"]
