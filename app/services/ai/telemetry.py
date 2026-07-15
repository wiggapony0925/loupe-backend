"""Loupe AI telemetry — clean ask logs, feedback capture, admin analytics.

Every "describe it" ask leaves two traces:

* one structured, grep-friendly INFO line (``ai.search ask …``) — the clean
  console log;
* one :class:`~app.models.ai_search_log.AiSearchLog` row — the flight
  recorder behind the /admin/ai conversations dev tool and the thumbs
  up/down accuracy analytics.

Persistence is strictly best-effort: a logging hiccup must never break the
user's search.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_search_log import FEEDBACK_DOWN, FEEDBACK_UP, AiSearchLog
from app.models.user import User
from app.utils.logger import get_logger

logger = get_logger("services.ai.telemetry")

#: Asks newer than this count as an "open conversation" in the dev tool.
OPEN_CONVERSATION_WINDOW = timedelta(minutes=30)

#: How many shown cards to snapshot per ask (matches the client's shelf).
RESULTS_SNAPSHOT_MAX = 12


def _compact_card(card: dict[str, Any]) -> dict[str, Any]:
    """One shown card, boiled down to what the drill-in needs to render it."""
    images = card.get("images") or {}
    best = None
    for key in ("large", "normal", "small"):
        img = images.get(key) or {}
        if isinstance(img, dict) and img.get("url"):
            best = img["url"]
            break
    pricing = card.get("pricing_summary") or {}
    market = pricing.get("market") or {}
    return {
        "id": card.get("id"),
        "name": card.get("name"),
        "setName": card.get("set_name"),
        "rarity": card.get("rarity"),
        "imageUrl": best or card.get("image_url"),
        "price": market.get("amount") if isinstance(market, dict) else None,
    }


async def log_ask(
    db: AsyncSession,
    *,
    user: User,
    query: str,
    game_hint: str | None,
    body: dict[str, Any],
    latency_ms: int,
) -> uuid.UUID | None:
    """Record one ask (console line + DB row); returns the row id or ``None``.

    The id rides back to the client as ``askId`` so the thumbs up/down under
    the answer can attach a verdict to exactly this exchange.
    """
    source = str(body.get("source") or "ai")
    cache_hit = bool(body.get("cached"))
    result_count = int(body.get("total") or 0)
    logger.info(
        "ai.search ask user=%s game=%s source=%s cached=%s results=%d took=%dms q=%r",
        user.id,
        body.get("game") or game_hint or "all",
        source,
        cache_hit,
        result_count,
        latency_ms,
        query[:120],
    )
    try:
        row = AiSearchLog(
            user_id=user.id,
            query=query[:220],
            game_hint=game_hint,
            game=body.get("game"),
            source=source,
            cache_hit=cache_hit,
            message=body.get("message"),
            candidates=list(body.get("candidates") or []) or None,
            results=[
                _compact_card(c)
                for c in list(body.get("results") or [])[:RESULTS_SNAPSHOT_MAX]
                if isinstance(c, dict)
            ]
            or None,
            result_count=result_count,
            latency_ms=latency_ms,
        )
        db.add(row)
        await db.commit()
        return row.id
    except Exception as exc:  # pragma: no cover - never fail the search
        logger.warning("ai.search ask log write failed: %s", exc)
        await db.rollback()
        return None


async def set_feedback(
    db: AsyncSession, *, ask_id: uuid.UUID, user: User, verdict: str
) -> bool:
    """Attach a thumbs verdict (``up``/``down``) to the caller's own ask.

    Idempotent — tapping again (or changing your mind) overwrites. Returns
    ``False`` when the ask doesn't exist or belongs to someone else.
    """
    row = await db.get(AiSearchLog, ask_id)
    if row is None or row.user_id != user.id:
        return False
    row.feedback = FEEDBACK_UP if verdict == "up" else FEEDBACK_DOWN
    row.feedback_at = datetime.now(UTC)
    await db.commit()
    logger.info(
        "ai.search feedback user=%s ask=%s verdict=%s", user.id, ask_id, verdict
    )
    return True


def _row_dict(row: AiSearchLog, email: str | None = None) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "userId": str(row.user_id) if row.user_id else None,
        "userEmail": email,
        "query": row.query,
        "gameHint": row.game_hint,
        "game": row.game,
        "source": row.source,
        "cacheHit": row.cache_hit,
        "message": row.message,
        "candidates": row.candidates or [],
        "results": row.results or [],
        "resultCount": row.result_count,
        "latencyMs": row.latency_ms,
        "feedback": row.feedback,
        "feedbackAt": row.feedback_at.isoformat() if row.feedback_at else None,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


async def overview(db: AsyncSession) -> dict[str, Any]:
    """The /admin/ai headline numbers + currently-open conversations."""
    now = datetime.now(UTC)
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    day = select(
        func.count().label("asks"),
        func.count(func.distinct(AiSearchLog.user_id)).label("users"),
        func.coalesce(
            func.avg(case((AiSearchLog.cache_hit.is_(True), 1.0), else_=0.0)), 0
        ).label("cache_rate"),
        func.coalesce(
            func.avg(case((AiSearchLog.source == "ai", 1.0), else_=0.0)), 0
        ).label("ai_rate"),
        func.avg(AiSearchLog.latency_ms).label("avg_latency"),
    ).where(AiSearchLog.created_at >= day_ago)
    d = (await db.execute(day)).one()

    week = select(
        func.count(case((AiSearchLog.feedback == FEEDBACK_UP, 1))).label("up"),
        func.count(case((AiSearchLog.feedback == FEEDBACK_DOWN, 1))).label("down"),
    ).where(AiSearchLog.created_at >= week_ago)
    w = (await db.execute(week)).one()
    rated = (w.up or 0) + (w.down or 0)

    open_q = (
        select(AiSearchLog, User.email)
        .join(User, User.id == AiSearchLog.user_id, isouter=True)
        .where(AiSearchLog.created_at >= now - OPEN_CONVERSATION_WINDOW)
        .order_by(AiSearchLog.created_at.desc())
        .limit(60)
    )
    open_rows = (await db.execute(open_q)).all()
    # Group into per-user "open conversations", newest activity first.
    conversations: dict[str, dict[str, Any]] = {}
    for row, email in open_rows:
        key = str(row.user_id or "anonymous")
        convo = conversations.setdefault(
            key,
            {
                "userId": str(row.user_id) if row.user_id else None,
                "userEmail": email,
                "lastAskAt": row.created_at.isoformat() if row.created_at else None,
                "asks": [],
            },
        )
        convo["asks"].append(_row_dict(row, email))

    return {
        "asks24h": int(d.asks or 0),
        "users24h": int(d.users or 0),
        "cacheHitRate24h": round(float(d.cache_rate or 0), 3),
        "aiRate24h": round(float(d.ai_rate or 0), 3),
        "avgLatencyMs24h": int(d.avg_latency) if d.avg_latency is not None else None,
        "feedback7d": {
            "up": int(w.up or 0),
            "down": int(w.down or 0),
            "satisfaction": round(w.up / rated, 3) if rated else None,
        },
        "openConversations": list(conversations.values()),
    }


async def list_logs(
    db: AsyncSession,
    *,
    feedback: str | None = None,
    source: str | None = None,
    game: str | None = None,
    user_id: uuid.UUID | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Filterable ask history for the dev tool's log table."""
    query = select(AiSearchLog, User.email).join(
        User, User.id == AiSearchLog.user_id, isouter=True
    )
    count_q = select(func.count()).select_from(AiSearchLog)
    conds = []
    if feedback == "up":
        conds.append(AiSearchLog.feedback == FEEDBACK_UP)
    elif feedback == "down":
        conds.append(AiSearchLog.feedback == FEEDBACK_DOWN)
    elif feedback == "rated":
        conds.append(AiSearchLog.feedback.is_not(None))
    if source:
        conds.append(AiSearchLog.source == source)
    if game:
        conds.append(AiSearchLog.game == game)
    if user_id:
        conds.append(AiSearchLog.user_id == user_id)
    if q:
        conds.append(AiSearchLog.query.ilike(f"%{q}%"))
    for c in conds:
        query = query.where(c)
        count_q = count_q.where(c)
    total = (await db.execute(count_q)).scalar_one()
    rows = (
        await db.execute(
            query.order_by(AiSearchLog.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return {
        "items": [_row_dict(row, email) for row, email in rows],
        "total": int(total),
        "page": page,
        "pageSize": page_size,
    }


async def get_log(db: AsyncSession, log_id: uuid.UUID) -> dict[str, Any] | None:
    """One ask in full, plus the asker's other recent asks (the conversation)."""
    row = await db.get(AiSearchLog, log_id)
    if row is None:
        return None
    email = None
    if row.user_id:
        email = (
            await db.execute(select(User.email).where(User.id == row.user_id))
        ).scalar_one_or_none()
    detail = _row_dict(row, email)
    if row.user_id:
        siblings = (
            await db.execute(
                select(AiSearchLog)
                .where(AiSearchLog.user_id == row.user_id, AiSearchLog.id != row.id)
                .order_by(AiSearchLog.created_at.desc())
                .limit(20)
            )
        ).scalars()
        detail["conversation"] = [_row_dict(s, email) for s in siblings]
    else:
        detail["conversation"] = []
    return detail


__all__ = [
    "get_log",
    "list_logs",
    "log_ask",
    "overview",
    "set_feedback",
]
