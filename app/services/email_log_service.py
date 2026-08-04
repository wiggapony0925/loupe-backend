"""Email delivery log — the paper trail behind every send.

Pipeline writers (``record_queued`` / ``mark_sent`` / ``mark_failed`` /
``apply_provider_event``) open their own short DB sessions: they're called
from background tasks after the request session is long gone, and a logging
failure must never break a send — every writer swallows and logs exceptions.

Admin readers (``list_logs`` / ``stats`` / ``get_log``) use the caller's
request session like any other service.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.models.email_log import EMAIL_STATUSES, EmailLog
from app.models.user import User, UserSettings
from app.utils.logger import get_logger

logger = get_logger("email.log")

#: Webhook event → log status. Bounces/complaints also trigger suppression.
_PROVIDER_EVENT_STATUS = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.bounced": "bounced",
    "email.complained": "complained",
}


async def record_queued(
    *,
    to_email: str,
    subject: str,
    html: str | None,
    text: str | None,
    category: str | None,
    headers: dict[str, str] | None,
    from_email: str | None,
    idempotency_key: str | None,
    user_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Insert a ``queued`` row; returns its id (None if logging failed)."""
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            row = EmailLog(
                to_email=to_email,
                user_id=user_id,
                category=category,
                subject=subject[:300],
                html=html,
                text=text,
                headers=headers,
                from_email=from_email,
                idempotency_key=idempotency_key,
            )
            session.add(row)
            await session.commit()
            return row.id
    except Exception as exc:
        logger.warning("email log write failed (%s)", exc)
        return None


async def has_ever_sent(db: AsyncSession, user_id: uuid.UUID, category: str) -> bool:
    """True if this user already has a log row for ``category``.

    The once-per-*account* guard, for mail whose triggering condition stays
    true forever after (a full vault stays full). Use :func:`has_sent_key`
    instead whenever the event can recur for different objects — one category
    per user would silence every set completion after the first.

    Uses the delivery log rather than a cache so the guarantee survives a
    cache flush, and reads on the caller's session because the check happens
    inside a request. Rows in any state count, including ``failed``:
    re-sending on every subsequent request would be worse than missing one.
    """
    return (
        await db.execute(
            select(EmailLog.id)
            .where(EmailLog.user_id == user_id, EmailLog.category == category)
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def has_sent_key(db: AsyncSession, idempotency_key: str) -> bool:
    """True if a log row already carries ``idempotency_key``.

    The once-per-*thing* guard: key on the entity (``set-complete-{user}-{set}``)
    and a collector gets one trophy per set rather than one per lifetime.
    Complements Resend's own 24h idempotency window, which is far too short
    for milestones that are re-evaluated on every page view.
    """
    return (
        await db.execute(
            select(EmailLog.id)
            .where(EmailLog.idempotency_key == idempotency_key)
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def _update(log_id: uuid.UUID | None, **values: Any) -> None:
    if log_id is None:
        return
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            row = await session.get(EmailLog, log_id)
            if row is None:
                return
            for key, value in values.items():
                setattr(row, key, value)
            await session.commit()
    except Exception as exc:
        logger.warning("email log update failed (%s)", exc)


async def mark_sent(log_id: uuid.UUID | None, provider_id: str | None) -> None:
    await _update(log_id, status="sent", provider_id=provider_id, error=None)


async def mark_failed(log_id: uuid.UUID | None, error: str) -> None:
    if log_id is None:
        return
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            row = await session.get(EmailLog, log_id)
            if row is None:
                return
            row.status = "failed"
            row.error = error[:500]
            row.attempts = (row.attempts or 0) + 1
            await session.commit()
    except Exception as exc:
        logger.warning("email log update failed (%s)", exc)


async def apply_provider_event(event_type: str, provider_id: str) -> str | None:
    """Advance the matching row from a Resend webhook event.

    Returns the recipient email when the event was a hard bounce/complaint
    (the caller then suppresses announcements for that address), else None.
    """
    status = _PROVIDER_EVENT_STATUS.get(event_type)
    if status is None or not provider_id:
        return None
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            row = (
                await session.execute(
                    select(EmailLog).where(EmailLog.provider_id == provider_id)
                )
            ).scalar_one_or_none()
            if row is None:
                logger.info("webhook for unknown provider_id=%s", provider_id)
                return None
            # Never regress a terminal state (delivered → sent, etc.).
            order = {s: i for i, s in enumerate(EMAIL_STATUSES)}
            if order.get(status, 0) > order.get(row.status, 0):
                row.status = status
                await session.commit()
            return row.to_email if status in ("bounced", "complained") else None
    except Exception as exc:
        logger.warning("email log webhook update failed (%s)", exc)
        return None


async def suppress_announcements(to_email: str) -> bool:
    """Hard bounce / complaint: stop announcement mail to this address."""
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            user = (
                await session.execute(
                    select(User).where(User.email == to_email.lower())
                )
            ).scalar_one_or_none()
            if user is None:
                return False
            settings_row = (
                await session.execute(
                    select(UserSettings).where(UserSettings.user_id == user.id)
                )
            ).scalar_one_or_none()
            if settings_row is None:
                settings_row = UserSettings(user_id=user.id)
                session.add(settings_row)
            settings_row.email_announcements_enabled = False
            await session.commit()
            logger.info(
                "announcements suppressed after bounce/complaint user=%s", user.id
            )
            return True
    except Exception as exc:
        logger.warning("suppression failed (%s)", exc)
        return False


# ── Admin readers (request session) ───────────────────────────────────────


async def list_logs(
    db: AsyncSession,
    *,
    status: str | None = None,
    category: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[EmailLog], int]:
    stmt = select(EmailLog)
    if status:
        stmt = stmt.where(EmailLog.status == status)
    if category:
        stmt = stmt.where(EmailLog.category == category)
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(EmailLog.to_email).like(needle),
                func.lower(EmailLog.subject).like(needle),
            )
        )
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(EmailLog.created_at.desc())
                .limit(min(limit, 100))
                .offset(max(offset, 0))
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def stats(db: AsyncSession) -> dict[str, int]:
    rows = (
        await db.execute(
            select(EmailLog.status, func.count()).group_by(EmailLog.status)
        )
    ).all()
    counts = dict.fromkeys(EMAIL_STATUSES, 0)
    for status, count in rows:
        counts[status] = int(count)
    return counts


async def get_log(db: AsyncSession, log_id: uuid.UUID) -> EmailLog | None:
    return await db.get(EmailLog, log_id)


__all__ = [
    "apply_provider_event",
    "get_log",
    "has_ever_sent",
    "list_logs",
    "mark_failed",
    "mark_sent",
    "record_queued",
    "stats",
    "suppress_announcements",
]
