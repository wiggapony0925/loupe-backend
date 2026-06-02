"""Set-completion progress for the signed-in user.

Computes, per `CardSet` the user has at least one graded card from,
how many distinct cards they own out of the set's published total.
Drives the "X/Y complete" rings on the vault screen.

We deliberately compute from local data only — every value is real and
user-scoped. Sets with `total_cards = NULL` (upstream hasn't told us
the size yet) fall back to the count of `cards` rows we have indexed
for that set, which is the most honest upper bound we can produce.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.grade import GradedCard
from app.models.user import User
from app.services.collection.portfolio_service import current_market_value


@dataclass(slots=True)
class SetProgress:
    set_id: str
    set_name: str
    set_code: str | None
    tcg: str
    image_url: str | None
    owned: int
    total: int
    percent: float  # 0.0 to 100.0
    estimated_value_usd: float
    missing_top: list[dict[str, Any]]  # up to 5 missing cards (id/name/number/image)

    def to_dict(self) -> dict[str, Any]:
        return {
            "setId": self.set_id,
            "setName": self.set_name,
            "setCode": self.set_code,
            "tcg": self.tcg,
            "imageUrl": self.image_url,
            "owned": self.owned,
            "total": self.total,
            "percent": round(self.percent, 2),
            "estimatedValueUsd": round(self.estimated_value_usd, 2),
            "missingTop": self.missing_top,
        }


async def list_progress(
    db: AsyncSession, user: User, *, missing_sample: int = 5
) -> list[dict[str, Any]]:
    """Return progress per set the user owns at least one card from."""
    # Step 1: load the user's graded cards joined to their `Card` row so
    # we can value each holding on the SAME basis as the vault total —
    # today's live market price (`pricing_summary.market.amount`) when
    # known, falling back to the scan-time `estimated_value_usd`. Summing
    # the stale scan-time estimate here is what made a single set read
    # higher than the entire portfolio (the vault total already moved to
    # live pricing); valuing both the same way keeps them consistent.
    rows = (
        await db.execute(
            select(GradedCard, Card)
            .join(Card, Card.id == GradedCard.card_id)
            .where(
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
            )
        )
    ).all()
    if not rows:
        return []

    # We count DISTINCT card_id so duplicates don't inflate "owned", but
    # sum value across every copy so two graded Charizards count twice.
    owned_by_set: dict[Any, set[Any]] = defaultdict(set)
    value_by_set: dict[Any, float] = defaultdict(float)
    for g, card in rows:
        sid = card.set_id
        if sid is None:
            continue
        owned_by_set[sid].add(card.id)
        live = current_market_value(card)
        if live is not None:
            value_by_set[sid] += float(live)
        elif g.estimated_value_usd is not None:
            value_by_set[sid] += float(g.estimated_value_usd)

    set_ids = list(owned_by_set.keys())
    sets = {
        s.id: s
        for s in (
            await db.execute(select(CardSet).where(CardSet.id.in_(set_ids)))
        ).scalars()
    }

    # Step 2: fallback totals (count of indexed cards) when `total_cards`
    # is null.
    fallback_total_rows = (
        await db.execute(
            select(Card.set_id, func.count(Card.id))
            .where(Card.set_id.in_(set_ids))
            .group_by(Card.set_id)
        )
    ).all()
    fallback_totals = {row[0]: row[1] for row in fallback_total_rows}

    out: list[SetProgress] = []
    for sid in set_ids:
        cs = sets.get(sid)
        if cs is None:
            continue
        owned = len(owned_by_set[sid])
        total = cs.total_cards or fallback_totals.get(sid, owned)
        percent = (owned / total * 100.0) if total > 0 else 0.0

        # Step 3: a small sample of missing cards in the set, so the UI
        # can render "you're missing Charizard, Pikachu, …" without an
        # extra round-trip. Capped to avoid loading a 500-card set.
        owned_ids = owned_by_set.get(sid, set())
        missing_rows = (
            await db.execute(
                select(Card.id, Card.name, Card.number, Card.image_url)
                .where(
                    Card.set_id == sid,
                    Card.id.not_in(owned_ids) if owned_ids else (Card.id == Card.id),
                )
                .order_by(Card.number.nulls_last(), Card.name)
                .limit(missing_sample)
            )
        ).all()
        missing_top = [
            {
                "cardId": str(r[0]),
                "name": r[1],
                "number": r[2],
                "imageUrl": r[3],
            }
            for r in missing_rows
        ]

        out.append(
            SetProgress(
                set_id=str(cs.id),
                set_name=cs.name,
                set_code=cs.code,
                tcg=cs.tcg.value if hasattr(cs.tcg, "value") else str(cs.tcg),
                image_url=cs.image_url,
                owned=owned,
                total=int(total),
                percent=percent,
                estimated_value_usd=value_by_set[sid],
                missing_top=missing_top,
            )
        )

    # Highest completion first so the vault rail leads with "almost done"
    out.sort(key=lambda s: (-s.percent, -s.owned))
    return [s.to_dict() for s in out]


__all__ = ["SetProgress", "list_progress"]
