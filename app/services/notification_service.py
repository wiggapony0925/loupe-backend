"""The inbox: create notifications, read them back, mark them read.

One rule shapes this module — **the row is the notification, push is just
delivery**. Everything the product wants to tell a user is written here first
and pushed second, so a user with no device, push switched off, or a phone
that was in a drawer still finds it waiting in the app. That also means the
unread badge is computed from durable rows rather than device storage, so it
survives a reinstall and agrees across a user's devices.

Writers should call :func:`notify` (one user) or :func:`broadcast` (many).
Both are best-effort at the *push* layer and strict at the *row* layer: a
failed push is logged and the notification still exists, while a duplicate
row is refused outright by ``dedupe_key``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    CATEGORY_CATALOGUE,
    CATEGORY_SYSTEM,
    Notification,
)
from app.models.user import User, UserSettings
from app.services import push_service
from app.utils.logger import get_logger

logger = get_logger("notifications")

#: Hard cap on a single page — the mobile inbox asks for 25.
MAX_PAGE_SIZE = 100

#: Fan-out chunk when broadcasting. Keeps one transaction from ballooning on a
#: large user table while still being far cheaper than a row-at-a-time loop.
_BROADCAST_CHUNK = 500


async def notify(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    category: str,
    kind: str,
    title: str,
    body: str | None = None,
    href: str | None = None,
    image_url: str | None = None,
    data: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    push: bool = True,
) -> Notification | None:
    """Record one notification and (optionally) push it.

    Returns the row, or ``None`` when ``dedupe_key`` matched an existing
    notification — callers can treat None as "already told them" rather than
    an error, which is what makes this safe to call from replayed webhooks
    and re-run crons.
    """
    row = Notification(
        user_id=user_id,
        category=category,
        kind=kind,
        title=title[:200],
        body=body,
        href=href,
        image_url=image_url,
        data=data,
        dedupe_key=dedupe_key,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # dedupe_key collision — this event was already delivered.
        await db.rollback()
        logger.info("notification deduped key=%s user=%s", dedupe_key, user_id)
        return None
    await db.refresh(row)

    if push:
        await _push(row, title=title, body=body)
    return row


async def _push(row: Notification, *, title: str, body: str | None) -> None:
    """Hand one notification to the push transport. Never raises.

    ``push_service`` already honors ``push_notifications_enabled`` and prunes
    dead tokens, so this only has to record whether the handoff happened —
    ``pushed_at`` is what tells an operator "this reached a device" versus
    "this is sitting unread in the app".
    """
    try:
        sent = await push_service.send_to_user(
            row.user_id,
            title=title,
            body=body or "",
            data={"notificationId": str(row.id), "href": row.href or ""},
        )
        if sent:
            # Its own short session: the caller's may already be closed, and a
            # bookkeeping write must never roll back the notification itself.
            from app.db import get_sessionmaker

            async with get_sessionmaker()() as session:
                await session.execute(
                    update(Notification)
                    .where(Notification.id == row.id)
                    .values(pushed_at=datetime.now(UTC))
                )
                await session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("push failed for notification=%s (%s)", row.id, exc)


async def broadcast(
    db: AsyncSession,
    *,
    category: str,
    kind: str,
    title: str,
    body: str | None = None,
    href: str | None = None,
    image_url: str | None = None,
    data: dict[str, Any] | None = None,
    dedupe_prefix: str | None = None,
    push: bool = True,
    only_push_enabled: bool = False,
) -> int:
    """Send the same notification to every active user. Returns rows created.

    ``dedupe_prefix`` keys each row as ``"{prefix}:{user_id}"``, so publishing
    the same article twice can't double-notify anyone — the per-user key is
    what makes that work without a separate "already sent" table.

    Banned and deleted accounts are excluded. ``only_push_enabled`` narrows to
    users who accept push; the default is False because the in-app inbox
    should still receive it even when the phone shouldn't buzz.

    .. warning::
       On a duplicate this rolls the session back to retry row-by-row, which
       **expires every instance the caller is holding** in that session. Read
       any attribute you still need (ids especially) *before* calling, or pass
       a session you don't mind losing state on.
    """
    stmt = select(User.id).where(User.deleted_at.is_(None), User.banned_at.is_(None))
    if only_push_enabled:
        stmt = stmt.outerjoin(UserSettings, UserSettings.user_id == User.id).where(
            (UserSettings.user_id.is_(None))
            | (UserSettings.push_notifications_enabled.is_(True))
        )
    user_ids = list((await db.execute(stmt)).scalars().all())

    # Build from plain values, never from the ORM objects: the retry path below
    # runs *after* a rollback, and reading an attribute off a rolled-back
    # instance triggers a lazy refresh (MissingGreenlet on the async engine).
    def _fields(uid: uuid.UUID) -> dict[str, Any]:
        return {
            "user_id": uid,
            "category": category,
            "kind": kind,
            "title": title[:200],
            "body": body,
            "href": href,
            "image_url": image_url,
            "data": data,
            "dedupe_key": f"{dedupe_prefix}:{uid}" if dedupe_prefix else None,
        }

    created = 0
    # user_id -> notification id, so a successful push can be recorded against
    # the row it delivered. Safe to hold across the commit because the
    # sessionmaker sets expire_on_commit=False (app/db/session.py:110).
    row_ids: dict[uuid.UUID, uuid.UUID] = {}
    for start in range(0, len(user_ids), _BROADCAST_CHUNK):
        chunk = [_fields(uid) for uid in user_ids[start : start + _BROADCAST_CHUNK]]
        rows = [Notification(**f) for f in chunk]
        db.add_all(rows)
        try:
            await db.commit()
            created += len(chunk)
            row_ids.update({r.user_id: r.id for r in rows})
        except IntegrityError:
            # At least one user in this chunk already has it. Fall back to
            # per-row inserts so the others still land — an all-or-nothing
            # chunk would let one duplicate silence hundreds of users.
            await db.rollback()
            inserted = await _insert_individually(db, chunk)
            created += len(inserted)
            row_ids.update(inserted)

    if push and created:
        # Record delivery, exactly as the single-notification path does in
        # `_push`. This loop used to call send_to_user and throw the answer
        # away, so every one of the 553 broadcast rows in production reads as
        # undelivered even when Expo accepted it and the phone buzzed —
        # `pushed_at` is the only thing that distinguishes "reached a device"
        # from "sitting unread", and it was permanently NULL.
        pushed: list[uuid.UUID] = []
        for uid in user_ids:
            try:
                sent = await push_service.send_to_user(
                    uid, title=title, body=body or ""
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("broadcast push failed user=%s (%s)", uid, exc)
                continue
            if sent and uid in row_ids:
                pushed.append(row_ids[uid])

        if pushed:
            # One statement for the whole broadcast rather than per user, and
            # in its own session for the same reason `_push` uses one: a
            # bookkeeping write must never roll back the notifications.
            from app.db import get_sessionmaker

            try:
                async with get_sessionmaker()() as session:
                    await session.execute(
                        update(Notification)
                        .where(Notification.id.in_(pushed))
                        .values(pushed_at=datetime.now(UTC))
                    )
                    await session.commit()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("broadcast pushed_at update failed (%s)", exc)

    logger.info("broadcast '%s' → %d/%d users", title, created, len(user_ids))
    return created


async def _insert_individually(
    db: AsyncSession, fields: list[dict[str, Any]]
) -> dict[uuid.UUID, uuid.UUID]:
    """Retry a failed chunk one row at a time, skipping duplicates.

    Takes plain dicts rather than ORM instances — the caller has just rolled
    back, so any instance from that chunk is expired and would hit the
    database again the moment an attribute is read.

    Returns ``{user_id: notification_id}`` for the rows that landed, so the
    caller can record delivery against them. It used to return a bare count,
    which meant any user who fell down this path could never have `pushed_at`
    set even after a successful push.
    """
    inserted: dict[uuid.UUID, uuid.UUID] = {}
    for f in fields:
        row = Notification(**f)
        db.add(row)
        try:
            await db.commit()
            inserted[row.user_id] = row.id
        except IntegrityError:
            await db.rollback()
    return inserted


async def list_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 25,
    category: str | None = None,
    unread_only: bool = False,
) -> tuple[list[Notification], int]:
    """One page of a user's inbox, newest first, plus the total count."""
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    where = [Notification.user_id == user_id]
    if category:
        where.append(Notification.category == category)
    if unread_only:
        where.append(Notification.read_at.is_(None))

    total = (
        await db.execute(select(func.count()).select_from(Notification).where(*where))
    ).scalar_one()
    rows = (
        (
            await db.execute(
                select(Notification)
                .where(*where)
                # id as a tiebreaker: two notifications created in the same
                # transaction share a timestamp, and an unstable sort would
                # let a row repeat on page 2 or vanish entirely.
                .order_by(Notification.created_at.desc(), Notification.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Badge number — unread rows for this user."""
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            )
        ).scalar_one()
    )


async def summary(db: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """The whole inbox header in one round trip.

    The clients render a filter strip with a badge per category. Built from
    the list endpoint that would be one request per tab *plus* one for the
    total — five requests to draw a header, refetched on every focus. This
    is a single GROUP BY, and it also hands back the category catalogue so
    the strip itself is server-defined rather than hardcoded three times.

    Billing is folded into "Account": it is real as a stored category (so
    receipts stay queryable) but it is not worth a tab of its own.
    """
    rows = (
        await db.execute(
            select(Notification.category, func.count())
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            .group_by(Notification.category)
        )
    ).all()
    counts = {str(category): int(n) for category, n in rows}

    # Categories with no tab of their own still have to be counted somewhere,
    # or the tab badges silently fail to add up to the app icon badge.
    shown = {c["key"] for c in CATEGORY_CATALOGUE}
    counts[CATEGORY_SYSTEM] = counts.get(CATEGORY_SYSTEM, 0) + sum(
        n for key, n in counts.items() if key not in shown
    )

    return {
        "unread": sum(int(n) for _, n in rows),
        "categories": [
            {**meta, "unread": counts.get(meta["key"], 0)}
            for meta in CATEGORY_CATALOGUE
        ],
    }


async def mark_read(db: AsyncSession, user_id: uuid.UUID, ids: list[uuid.UUID]) -> int:
    """Mark specific notifications read. Scoped to the owner."""
    if not ids:
        return 0
    result = await db.execute(
        update(Notification)
        .where(
            Notification.user_id == user_id,
            Notification.id.in_(ids),
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )
    await db.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Clear the badge."""
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await db.commit()
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "MAX_PAGE_SIZE",
    "broadcast",
    "list_for_user",
    "mark_all_read",
    "mark_read",
    "notify",
    "summary",
    "unread_count",
]
