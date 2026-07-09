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

import re
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


def _num_key(number: str | None) -> str:
    """Canonical collector-number key for owned/missing matching.

    Takes the numerator only and drops leading zeros so both sides of the join
    line up regardless of how each stored it: "058/102" → "58", "4/102" → "4"
    (NOT "4102" — stripping all non-digits would fold the denominator in),
    "TG12/TG30" → "12". Empty when there's no number.
    """
    head = str(number or "").split("/")[0]
    return "".join(ch for ch in head if ch.isdigit()).lstrip("0")


def _norm_set_name(name: str | None) -> str:
    """Set name reduced for fuzzy matching ("Base Set" → "base")."""
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    if s.endswith(" set"):
        s = s[: -len(" set")].strip()
    return s


async def _resolve_mirror_set_id(db: AsyncSession, cs: CardSet) -> str | None:
    """Best-effort bridge from a local ``CardSet`` to its catalog-mirror id.

    The mirror keys on the provider's own set id and stores the provider's set
    name, neither of which our local rows always carry verbatim — so we cascade
    from the most precise key to the fuzziest:

    1. ``code`` == mirror id — modern sets store the pokemontcg id in ``code``
       ("sv3pt5", "cel25c"): exact and unambiguous.
    2. exact (case-insensitive) name — "151", "Obsidian Flames".
    3. normalized name disambiguated by card total — rescues legacy sets the
       provider names differently ("Base Set"/102 → mirror "Base"/102, never
       "Base Set 2"/130).
    """
    if cs.code:
        hit = (
            await db.execute(
                select(CatalogMirrorSet.id)
                .where(func.lower(CatalogMirrorSet.id) == cs.code.lower())
                .limit(1)
            )
        ).scalar_one_or_none()
        if hit:
            return hit

    if cs.name:
        hit = (
            await db.execute(
                select(CatalogMirrorSet.id)
                .where(func.lower(CatalogMirrorSet.name) == cs.name.lower())
                .limit(1)
            )
        ).scalar_one_or_none()
        if hit:
            return hit

    norm = _norm_set_name(cs.name)
    if not norm:
        return None
    # Small table (~200 pokemon sets) — normalize + total-match in Python
    # rather than fight cross-dialect string functions.
    candidates = (
        await db.execute(
            select(CatalogMirrorSet.id, CatalogMirrorSet.name, CatalogMirrorSet.total)
        )
    ).all()
    matches = [c for c in candidates if _norm_set_name(c[1]) == norm]
    if not matches:
        return None
    if cs.total_cards is not None:
        exact = [c for c in matches if c[2] == cs.total_cards]
        if exact:
            return str(exact[0][0])
    return str(matches[0][0])


async def set_checklist(
    db: AsyncSession, user: User, set_id: uuid.UUID
) -> dict[str, Any]:
    """The full card checklist for one set — every card, flagged owned/missing.

    Backs the "tap a set → have + still-missing" sheet. The complete set list
    comes from the catalog mirror when we can bridge to it (Pokémon). For sets
    with no mirror coverage (Magic/Yu-Gi-Oh, or an unmatched set) we fall back
    to the local ``Card`` table so at least every owned card still renders —
    an honest partial list beats an empty one.
    """
    cs = (
        await db.execute(select(CardSet).where(CardSet.id == set_id))
    ).scalar_one_or_none()
    if cs is None:
        raise HTTPException(status_code=404, detail="Set not found")

    # The user's owned cards in this set — both bare collector number (to match
    # mirror rows) and local card id (to match local rows).
    owned_rows = (
        await db.execute(
            select(Card.id, Card.number)
            .select_from(GradedCard)
            .join(Card, Card.id == GradedCard.card_id)
            .where(
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
                Card.set_id == set_id,
            )
        )
    ).all()
    owned_keys = {_num_key(r[1]) for r in owned_rows if _num_key(r[1])}
    owned_ids = {r[0] for r in owned_rows}

    cards: list[dict[str, Any]] = []
    mirror_set_id = await _resolve_mirror_set_id(db, cs)
    if mirror_set_id is not None:
        rows = (
            (
                await db.execute(
                    select(CatalogMirrorCard)
                    .where(CatalogMirrorCard.set_id == mirror_set_id)
                    .order_by(
                        CatalogMirrorCard.number_int.nulls_last(),
                        CatalogMirrorCard.bare_number,
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
            key = _num_key(mc.bare_number or mc.number)
            cards.append(
                {
                    # ``mc.id`` is already the composite "<source>:<upstream_id>"
                    # (e.g. "pokemontcg:base1-4") the card-detail route expects.
                    "id": mc.id,
                    "name": mc.name,
                    "number": mc.number,
                    "imageUrl": img,
                    "owned": bool(key and key in owned_keys),
                }
            )
    else:
        # No mirror coverage — build from whatever we've indexed locally.
        local = (
            (
                await db.execute(
                    select(Card)
                    .where(Card.set_id == set_id)
                    .order_by(Card.number.nulls_last(), Card.name)
                )
            )
            .scalars()
            .all()
        )
        for c in local:
            cards.append(
                {
                    "id": str(c.id),
                    "name": c.name,
                    "number": c.number,
                    "imageUrl": c.image_url,
                    "owned": c.id in owned_ids,
                }
            )

    owned_count = sum(1 for c in cards if c["owned"])
    total = len(cards) or int(cs.total_cards or 0)
    return {
        "setId": str(cs.id),
        "setName": cs.name,
        "total": total,
        "owned": owned_count,
        "cards": cards,
    }


__all__ = ["SetProgress", "list_progress", "set_checklist"]
