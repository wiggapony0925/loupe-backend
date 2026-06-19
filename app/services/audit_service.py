"""Audit logging — append-only records of sensitive (admin) actions.

Writes to :class:`app.models.audit.AuditLog`. Best-effort: an audit failure
must never break the action it describes, so callers can ignore exceptions
(this commits its own row independently of the action's transaction).
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.user import User
from app.utils.logger import get_logger

logger = get_logger("audit")


def client_ip(request: Request | None) -> str | None:
    """Best-effort client IP, honouring a single proxy hop (X-Forwarded-For)."""
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip() or None
    return request.client.host if request.client else None


async def record(
    db: AsyncSession,
    *,
    request: Request | None,
    user: User | None,
    action: str,
    target_table: str | None = None,
    target_id: Any = None,
    payload: dict | None = None,
) -> None:
    """Append an audit row. Swallows errors — auditing is never load-bearing."""
    try:
        db.add(
            AuditLog(
                user_id=user.id if user else None,
                action=action,
                target_table=target_table,
                target_id=str(target_id) if target_id is not None else None,
                payload=payload,
                ip_address=client_ip(request),
            )
        )
        await db.commit()
    except Exception as exc:  # pragma: no cover - audit must not break callers
        logger.warning("audit write failed for action=%s: %s", action, exc)


__all__ = ["client_ip", "record"]
