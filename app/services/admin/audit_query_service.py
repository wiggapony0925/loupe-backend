"""Read side of the audit trail — paginated, filterable queries for the viewer.

The write path lives in :mod:`app.services.audit_service`; this only reads the
append-only ``audit_log`` table, joining the actor's email for display.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.user import User
from app.schemas.ops import AuditEntry, AuditFacets, AuditPage

_MAX_PAGE_SIZE = 100


async def page(
    db: AsyncSession,
    *,
    action: str | None = None,
    target_table: str | None = None,
    actor: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> AuditPage:
    """A page of audit records, newest first, with optional filters."""
    page = max(page, 1)
    page_size = max(1, min(page_size, _MAX_PAGE_SIZE))

    filters = []
    if action:
        filters.append(AuditLog.action == action)
    if target_table:
        filters.append(AuditLog.target_table == target_table)
    if actor:
        filters.append(User.email.ilike(f"%{actor}%"))

    base = select(AuditLog, User.email).outerjoin(User, AuditLog.user_id == User.id)
    for f in filters:
        base = base.where(f)

    total = await db.scalar(
        select(func.count()).select_from(base.order_by(None).subquery())
    )

    rows = (
        await db.execute(
            base.order_by(AuditLog.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    results = [
        AuditEntry(
            id=log.id,
            user_id=log.user_id,
            actor_email=email,
            action=log.action,
            target_table=log.target_table,
            target_id=log.target_id,
            payload=log.payload,
            ip_address=log.ip_address,
            created_at=log.created_at,
        )
        for log, email in rows
    ]
    return AuditPage(
        results=results, total=int(total or 0), page=page, page_size=page_size
    )


async def facets(db: AsyncSession) -> AuditFacets:
    """Distinct actions and target tables — populates the filter dropdowns."""
    actions = (
        (await db.execute(select(AuditLog.action).distinct().order_by(AuditLog.action)))
        .scalars()
        .all()
    )
    tables = (
        (
            await db.execute(
                select(AuditLog.target_table)
                .where(AuditLog.target_table.is_not(None))
                .distinct()
                .order_by(AuditLog.target_table)
            )
        )
        .scalars()
        .all()
    )
    return AuditFacets(actions=list(actions), tables=[t for t in tables if t])


__all__ = ["facets", "page"]
