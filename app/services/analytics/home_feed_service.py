"""Home-feed aggregation — server-side rails for the Command tab.

Builds the data the mobile app's Home screen renders in a single
authenticated round-trip:

* ``topMovers``  – the user's owned cards ranked by trailing 1-year
  price change. Computed pure-DB from the same ``price_history`` that
  feeds the portfolio chart, so no N+1 fan-out to ``/cards/{id}/market``.
* ``recentScans`` – the user's most recently graded cards, in scan order.

Hot-Right-Now is intentionally **not** included: it's a public search
query (``/cards/search?q=charizard``) that doesn't depend on the user
and is already cached aggressively at the edge.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.grade import GradedCard
from app.models.user import User
from app.services.collection import collection_service
from app.services.collection.portfolio_service import _extract_price_history, _value_on


def _change_1y(
    card: Card | None, estimated_value_usd: float | None
) -> tuple[float, float] | None:
    """Compute the trailing 1-year change from real price_history.

    Returns ``(pct, usd)`` — the % change AND the absolute dollar move from
    the SAME history baseline, so no client ever back-derives one from the
    other. Returns ``None`` when we don't have enough history to make an
    honest claim (less than two distinct dated points covering >= 30 days).
    """
    hist = _extract_price_history(card)
    if len(hist) < 2:
        return None
    today = datetime.now(UTC).date()
    a_year_ago = today - timedelta(days=365)
    # Skip cards whose entire history is younger than 30 days — change_pct
    # over a 2-week sample is noise, not signal.
    span_days = (hist[-1][0] - hist[0][0]).days
    if span_days < 30:
        return None
    price_now = _value_on(None, hist, today)
    price_then = _value_on(None, hist, a_year_ago)
    if price_then <= 0:
        return None
    pct = (price_now - price_then) / price_then * 100.0
    return round(pct, 2), round(price_now - price_then, 2)


def _change_pct_1y(
    card: Card | None, estimated_value_usd: float | None
) -> float | None:
    """Trailing 1-year change % (see :func:`_change_1y`)."""
    change = _change_1y(card, estimated_value_usd)
    return change[0] if change is not None else None


async def top_movers(
    db: AsyncSession,
    user: User,
    enrich_limit: int = 12,
    limit: int = 5,
    collection_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Top |change_pct_1y| movers across the user's distinct owned cards.

    ``enrich_limit`` caps how many distinct cards we score per request
    (cheap upper bound on price_history extraction); ``limit`` caps the
    rows returned to the client. ``collection_id`` scopes the movers to a
    single collection via the same ``holdings_scope`` seam the dashboard uses.
    """
    scope = collection_service.holdings_scope(collection_id, user)
    stmt = (
        select(GradedCard, Card, CardSet)
        .outerjoin(Card, Card.id == GradedCard.card_id)
        .outerjoin(CardSet, CardSet.id == Card.set_id)
        .where(GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None))
        .order_by(GradedCard.graded_at.desc().nulls_last())
    )
    if scope is not None:
        stmt = stmt.where(scope)
    rows = (await db.execute(stmt)).all()

    seen: set[str] = set()
    scored: list[dict[str, Any]] = []
    for g, c, s in rows:
        cid = str(g.card_id) if g.card_id is not None else None
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        est = (
            float(g.estimated_value_usd) if g.estimated_value_usd is not None else None
        )
        change = _change_1y(c, est)
        scored.append(
            {
                "gradeId": str(g.id),
                "cardId": cid,
                "cardName": c.name if c is not None else None,
                "cardImageUrl": c.image_url if c is not None else None,
                "cardNumber": c.number if c is not None else None,
                "cardYear": c.year if c is not None else None,
                "cardTcg": (
                    c.tcg.value
                    if c is not None and hasattr(c.tcg, "value")
                    else (str(c.tcg) if c is not None else None)
                ),
                "cardSetName": s.name if s is not None else None,
                "priceUsd": est,
                "changePct1y": change[0] if change is not None else None,
                # Absolute 1Y move from the SAME history baseline — clients
                # must render this, never back-derive dollars from the %.
                "changeUsd1y": change[1] if change is not None else None,
            }
        )
        if len(scored) >= enrich_limit:
            break

    # Sort by |change_pct| desc; rows without history sink to the bottom.
    scored.sort(key=lambda r: abs(r["changePct1y"] or -1), reverse=True)
    return scored[:limit]


async def recent_scans(
    db: AsyncSession,
    user: User,
    limit: int = 6,
    collection_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Most recently graded cards in scan-time order (optionally scoped)."""
    scope = collection_service.holdings_scope(collection_id, user)
    stmt = (
        select(GradedCard, Card, CardSet)
        .outerjoin(Card, Card.id == GradedCard.card_id)
        .outerjoin(CardSet, CardSet.id == Card.set_id)
        .where(GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None))
        .order_by(GradedCard.graded_at.desc().nulls_last(), GradedCard.id.desc())
        .limit(limit)
    )
    if scope is not None:
        stmt = stmt.where(scope)
    rows = (await db.execute(stmt)).all()
    out: list[dict[str, Any]] = []
    for g, c, s in rows:
        out.append(
            {
                "gradeId": str(g.id),
                "cardId": str(g.card_id) if g.card_id is not None else None,
                "cardName": c.name if c is not None else None,
                "cardImageUrl": c.image_url if c is not None else None,
                "cardSetName": s.name if s is not None else None,
                "grade": float(g.grade) if g.grade is not None else None,
                "house": (
                    g.house.value if hasattr(g.house, "value") else str(g.house or "")
                ).lower()
                or None,
                "scannedAt": g.graded_at.isoformat()
                if g.graded_at is not None
                else None,
                "estimatedValueUsd": (
                    float(g.estimated_value_usd)
                    if g.estimated_value_usd is not None
                    else None
                ),
            }
        )
    return out


async def build_feed(
    db: AsyncSession,
    user: User,
    *,
    top_movers_limit: int = 5,
    recent_scans_limit: int = 6,
    collection_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    movers = await top_movers(
        db, user, limit=top_movers_limit, collection_id=collection_id
    )
    recent = await recent_scans(
        db, user, limit=recent_scans_limit, collection_id=collection_id
    )
    return {"topMovers": movers, "recentScans": recent}


__all__ = ["build_feed", "recent_scans", "top_movers"]
