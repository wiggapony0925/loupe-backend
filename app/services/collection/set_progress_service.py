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

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.catalog_mirror import CatalogMirrorCard, CatalogMirrorSet
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


def _bare(number: str | None) -> str:
    """Collector number reduced to bare digits ("058/102" → "58")."""
    return "".join(ch for ch in str(number or "") if ch.isdigit())


async def set_checklist(
    db: AsyncSession, user: User, set_id: uuid.UUID
) -> dict[str, Any]:
    """The full card checklist for one set — every card, flagged owned/missing.

    Backs the "tap a set → have + still-missing" sheet. The complete set list
    comes from the catalog mirror (the local ``Card`` table only holds cards
    someone has already viewed/owned, so it can't show what you're missing).
    A card is "owned" when the user holds a copy with the same collector number
    in this set.
    """
    cs = (
        await db.execute(select(CardSet).where(CardSet.id == set_id))
    ).scalar_one_or_none()
    if cs is None:
        raise HTTPException(status_code=404, detail="Set not found")

    owned_rows = (
        await db.execute(
            select(Card.number)
            .select_from(GradedCard)
            .join(Card, Card.id == GradedCard.card_id)
            .where(
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
                Card.set_id == set_id,
            )
        )
    ).all()
    owned_bare = {_bare(r[0]) for r in owned_rows if r[0]}

    mirror_set_id = (
        await db.execute(
            select(CatalogMirrorSet.id)
            .where(func.lower(CatalogMirrorSet.name) == (cs.name or "").lower())
            .limit(1)
        )
    ).scalar_one_or_none()

    cards: list[dict[str, Any]] = []
    if mirror_set_id is not None:
        rows = (
            (
                await db.execute(
                    select(CatalogMirrorCard)
                    .where(CatalogMirrorCard.set_id == mirror_set_id)
                    .order_by(
                        CatalogMirrorCard.bare_number.nulls_last(),
                        CatalogMirrorCard.name,
                    )
                )
            )
            .scalars()
            .all()
        )
        for mc in rows:
            payload = mc.payload or {}
            img = (payload.get("images") or {}).get("small") or payload.get("image")
            cards.append(
                {
                    "id": f"{mc.source}:{mc.id}",
                    "name": mc.name,
                    "number": mc.number,
                    "imageUrl": img,
                    "owned": bool(mc.bare_number and mc.bare_number in owned_bare),
                }
            )

    owned_count = sum(1 for c in cards if c["owned"])
    return {
        "setId": str(cs.id),
        "setName": cs.name,
        "total": len(cards) or int(cs.total_cards or 0),
        "owned": owned_count,
        "cards": cards,
    }


__all__ = ["SetProgress", "list_progress", "set_checklist"]
