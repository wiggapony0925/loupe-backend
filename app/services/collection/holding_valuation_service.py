"""Seed a holding's ``estimated_value_usd`` from live market data.

A holding's value is ``GradedCard.estimated_value_usd`` — see
:func:`app.services.collection.portfolio_service.holding_value_usd`, which every
collection total runs through. Nothing populated that field unless the client
sent one, so a quick-add (which sends only the card, grade and condition) landed
a holding worth ``NULL`` → ``$0``: the card row showed no price and the vault
total never moved. This module is the one place that derives it.

**Honesty rules.** The only real price we hold for a card is the raw/loose
market price in ``Card.card_metadata['pricing_summary']['market']``. So:

* Raw holdings are valued at that price, scaled by a published condition
  factor (below). NM is the market price itself, unscaled.
* Slabbed holdings are valued at the raw price too — deliberately NOT marked
  up. The per-grade ladder in ``market_service`` is *modelled* (seeded RNG),
  and multiplying a real holding by a synthetic premium would put an invented
  number on the user's net worth. A slab is worth *at least* the raw card, so
  this is a floor, not a guess, and the owner can type the real number in.
* When no market price exists we return ``None`` and store ``NULL`` rather
  than ``0`` — "unknown" and "worthless" must not look the same, and
  ``holding_value_usd`` already treats ``NULL`` as contributing nothing.

A caller-supplied value always wins: if the owner (or the scan pipeline) says
what the slab is worth, that is the better number and we never overwrite it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from app.models.card import Card
from app.models.enums import RawConditionEnum

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CONDITION_FACTORS",
    "backfill_missing_values",
    "condition_factor",
    "derive_estimated_value",
    "market_price",
]


# Share of the NM market price a raw card in each condition typically fetches.
# These are valuation *assumptions*, not market data — they are deliberately
# conservative, documented here, and applied only to the raw price we actually
# hold. Roughly the spread used by the major singles marketplaces.
CONDITION_FACTORS: dict[RawConditionEnum, Decimal] = {
    RawConditionEnum.nm: Decimal("1.00"),
    RawConditionEnum.lp: Decimal("0.85"),
    RawConditionEnum.mp: Decimal("0.70"),
    RawConditionEnum.hp: Decimal("0.50"),
    RawConditionEnum.dmg: Decimal("0.30"),
}

_CENTS = Decimal("0.01")


def condition_factor(condition: RawConditionEnum | str | None) -> Decimal:
    """Multiplier for a raw condition. Unknown/absent conditions read as NM."""
    if condition is None:
        return CONDITION_FACTORS[RawConditionEnum.nm]
    if isinstance(condition, RawConditionEnum):
        return CONDITION_FACTORS.get(condition, CONDITION_FACTORS[RawConditionEnum.nm])
    try:
        return CONDITION_FACTORS[RawConditionEnum(str(condition).lower())]
    except (ValueError, KeyError):
        return CONDITION_FACTORS[RawConditionEnum.nm]


def market_price(card: Card | None) -> Decimal | None:
    """Live raw market price off the card's cached pricing summary.

    Mirrors ``portfolio_service.current_market_value`` but is kept here so the
    write path doesn't import the read path. Returns ``None`` for anything that
    isn't a usable positive number — a malformed upstream payload must not
    silently value a holding at zero.
    """
    if card is None or not isinstance(card.card_metadata, dict):
        return None
    pricing = card.card_metadata.get("pricing_summary")
    if not isinstance(pricing, dict):
        return None
    market = pricing.get("market")
    if not isinstance(market, dict):
        return None
    amount = market.get("amount")
    if amount is None or isinstance(amount, bool):
        return None
    try:
        value = Decimal(str(amount))
    except (ArithmeticError, ValueError, TypeError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


def derive_estimated_value(
    card: Card | None,
    *,
    condition: RawConditionEnum | str | None,
    explicit: Decimal | float | None = None,
) -> Decimal | None:
    """Value for a holding, or ``None`` when we genuinely don't know one.

    ``explicit`` (what the caller sent) always wins — including an explicit
    ``0``, which is a legitimate statement that a card is worthless and must
    not be "corrected" into a market price.
    """
    if explicit is not None:
        try:
            value = Decimal(str(explicit))
        except (ArithmeticError, ValueError, TypeError):
            return None
        return (
            value.quantize(_CENTS, rounding=ROUND_HALF_UP)
            if value.is_finite()
            else None
        )

    price = market_price(card)
    if price is None:
        return None
    return (price * condition_factor(condition)).quantize(
        _CENTS, rounding=ROUND_HALF_UP
    )


async def backfill_missing_values(db: AsyncSession, *, limit: int = 5_000) -> int:
    """Fill ``estimated_value_usd`` on holdings that never got one.

    Every holding created before valuation-on-create — and every quick-add
    since — stored ``NULL``, so those collections read ``$0`` forever. This
    heals them from the same live price a fresh add would use.

    Only ever writes rows where the column is ``NULL``: a holding the owner
    priced (including at ``0``) is left exactly as they set it. Safe to run
    repeatedly; returns how many rows it updated.
    """
    from sqlalchemy import select as _select

    from app.models.grade import GradedCard as _GradedCard

    rows = (
        await db.execute(
            _select(_GradedCard, Card)
            .join(Card, Card.id == _GradedCard.card_id)
            .where(
                _GradedCard.estimated_value_usd.is_(None),
                _GradedCard.deleted_at.is_(None),
            )
            .limit(limit)
        )
    ).all()

    updated = 0
    for holding, card in rows:
        value = derive_estimated_value(card, condition=holding.condition, explicit=None)
        if value is None:
            # No usable market price yet — leave NULL so a later run retries.
            continue
        holding.estimated_value_usd = value
        updated += 1

    if updated:
        await db.commit()
    return updated
