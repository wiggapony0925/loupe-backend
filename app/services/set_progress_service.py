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

from dataclasses import dataclass
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.grade import GradedCard
from app.models.user import User


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
    # Step 1: per-set aggregates from the user's graded cards. We count
    # DISTINCT card_id so duplicates don't inflate "owned".
    agg_rows = (
        await db.execute(
            select(
                Card.set_id,
                func.count(distinct(Card.id)).label("owned"),
                func.coalesce(
                    func.sum(GradedCard.estimated_value_usd), 0
                ).label("value"),
            )
            .join(GradedCard, GradedCard.card_id == Card.id)
            .where(
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
            )
            .group_by(Card.set_id)
        )
    ).all()
    if not agg_rows:
        return []

    set_ids = [row.set_id for row in agg_rows]
    sets = {
        s.id: s
        for s in (
            await db.execute(select(CardSet).where(CardSet.id.in_(set_ids)))
        ).scalars()
    }

    # Step 2: fallback totals (count of indexed cards) when `total_cards`
    # is null, plus the set of owned card ids per set for the missing-top
    # query.
    fallback_total_rows = (
        await db.execute(
            select(Card.set_id, func.count(Card.id))
            .where(Card.set_id.in_(set_ids))
            .group_by(Card.set_id)
        )
    ).all()
    fallback_totals = {row[0]: row[1] for row in fallback_total_rows}

    owned_ids_rows = (
        await db.execute(
            select(Card.set_id, Card.id)
            .join(GradedCard, GradedCard.card_id == Card.id)
            .where(
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
                Card.set_id.in_(set_ids),
            )
        )
    ).all()
    owned_by_set: dict[Any, set[Any]] = {}
    for sid, cid in owned_ids_rows:
        owned_by_set.setdefault(sid, set()).add(cid)

    out: list[SetProgress] = []
    for row in agg_rows:
        cs = sets.get(row.set_id)
        if cs is None:
            continue
        total = cs.total_cards or fallback_totals.get(row.set_id, row.owned)
        owned = int(row.owned)
        percent = (owned / total * 100.0) if total > 0 else 0.0

        # Step 3: a small sample of missing cards in the set, so the UI
        # can render "you're missing Charizard, Pikachu, …" without an
        # extra round-trip. Capped to avoid loading a 500-card set.
        owned_ids = owned_by_set.get(row.set_id, set())
        missing_rows = (
            await db.execute(
                select(Card.id, Card.name, Card.number, Card.image_url)
                .where(
                    Card.set_id == row.set_id,
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
                estimated_value_usd=float(row.value or 0),
                missing_top=missing_top,
            )
        )

    # Highest completion first so the vault rail leads with "almost done"
    out.sort(key=lambda s: (-s.percent, -s.owned))
    return [s.to_dict() for s in out]


__all__ = ["SetProgress", "list_progress"]
