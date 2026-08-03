"""Valuation rules for a holding's ``estimated_value_usd``.

These assert the behaviour that the "$0 collection" bug came from: a holding
created without a price must pick one up from live market data, and everything
that *isn't* a usable price must stay ``NULL`` rather than collapse to ``0``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.card import Card
from app.models.enums import RawConditionEnum
from app.services.collection import holding_valuation_service as hv


def _card(market: object) -> Card:
    """A Card carrying whatever the upstream pricing summary happened to be."""
    card = Card()
    card.card_metadata = {"pricing_summary": {"market": market}}
    return card


def _priced(amount: object) -> Card:
    return _card({"amount": amount, "currency": "USD"})


# ── the actual bug ────────────────────────────────────────────────────────


def test_quick_add_takes_the_live_market_price() -> None:
    """Raw + NM + no price sent — exactly what long-press quick-add posts."""
    value = hv.derive_estimated_value(
        _priced("2416.28"), condition=RawConditionEnum.nm, explicit=None
    )
    assert value == Decimal("2416.28")


def test_quick_add_is_not_zero() -> None:
    """Regression guard: this returned None → stored NULL → vault read $0."""
    value = hv.derive_estimated_value(
        _priced("12.50"), condition=RawConditionEnum.nm, explicit=None
    )
    assert value is not None and value > 0


# ── condition ladder ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (RawConditionEnum.nm, Decimal("100.00")),
        (RawConditionEnum.lp, Decimal("85.00")),
        (RawConditionEnum.mp, Decimal("70.00")),
        (RawConditionEnum.hp, Decimal("50.00")),
        (RawConditionEnum.dmg, Decimal("30.00")),
    ],
)
def test_condition_scales_the_price(
    condition: RawConditionEnum, expected: Decimal
) -> None:
    assert (
        hv.derive_estimated_value(_priced("100.00"), condition=condition, explicit=None)
        == expected
    )


def test_missing_condition_is_treated_as_nm() -> None:
    """Slabs carry no raw condition; they must not be valued at zero."""
    assert hv.derive_estimated_value(
        _priced("40.00"), condition=None, explicit=None
    ) == Decimal("40.00")


def test_unknown_condition_string_falls_back_to_nm() -> None:
    assert hv.derive_estimated_value(
        _priced("40.00"), condition="not-a-condition", explicit=None
    ) == Decimal("40.00")


def test_condition_accepts_a_raw_string() -> None:
    assert hv.derive_estimated_value(
        _priced("100.00"), condition="lp", explicit=None
    ) == Decimal("85.00")


# ── caller-supplied value wins ────────────────────────────────────────────


def test_explicit_value_beats_market_price() -> None:
    """The owner priced their slab; we must not overwrite it."""
    assert hv.derive_estimated_value(
        _priced("100.00"), condition=RawConditionEnum.nm, explicit=Decimal("950")
    ) == Decimal("950.00")


def test_explicit_zero_is_respected() -> None:
    """An explicit 0 means "worthless", not "unset" — don't re-price it."""
    assert hv.derive_estimated_value(
        _priced("100.00"), condition=RawConditionEnum.nm, explicit=0
    ) == Decimal("0.00")


# ── "unknown" must never become "zero" ────────────────────────────────────


@pytest.mark.parametrize(
    "card",
    [
        None,
        Card(),  # no card_metadata at all
        _card(None),
        _card({}),
        _card({"amount": None}),
        _priced("0"),  # a zero upstream price is not a real price
        _priced("-5"),
        _priced("not-a-number"),
        _priced(float("nan")),
        _priced(float("inf")),
        _priced(True),  # bool is an int subclass — must not read as 1
    ],
)
def test_unusable_price_yields_none_not_zero(card: Card | None) -> None:
    assert (
        hv.derive_estimated_value(card, condition=RawConditionEnum.nm, explicit=None)
        is None
    )


def test_metadata_that_is_not_a_dict_is_survivable() -> None:
    card = Card()
    card.card_metadata = "garbage"  # type: ignore[assignment]
    assert (
        hv.derive_estimated_value(card, condition=RawConditionEnum.nm, explicit=None)
        is None
    )


# ── money handling ────────────────────────────────────────────────────────


def test_result_is_rounded_to_cents() -> None:
    """0.85 × 33.33 = 28.3305 — must not persist sub-cent precision."""
    assert hv.derive_estimated_value(
        _priced("33.33"), condition=RawConditionEnum.lp, explicit=None
    ) == Decimal("28.33")


def test_half_up_rounding() -> None:
    assert hv.derive_estimated_value(
        _priced("0.005"), condition=RawConditionEnum.nm, explicit=None
    ) == Decimal("0.01")


def test_no_float_drift_on_large_values() -> None:
    """Decimal end-to-end: a naive float pipeline lands on 8499.999999."""
    assert hv.derive_estimated_value(
        _priced("9999.99"), condition=RawConditionEnum.lp, explicit=None
    ) == Decimal("8499.99")


def test_condition_factors_are_all_positive_fractions() -> None:
    for condition, factor in hv.CONDITION_FACTORS.items():
        assert Decimal("0") < factor <= Decimal("1"), condition


def test_every_condition_has_a_factor() -> None:
    """A new RawCondition must not silently value holdings at NM."""
    assert set(hv.CONDITION_FACTORS) == set(RawConditionEnum)


# ── backfill: the heal path ────────────────────────────────────────────────


async def _seed_card(db_session, *, name: str, market: object) -> Card:
    """A persisted Card carrying a pricing summary (needs a CardSet FK)."""
    from app.models.card import CardSet

    card_set = CardSet(name="Test Set", tcg="pokemon", code=f"ts-{name[:6].lower()}")
    db_session.add(card_set)
    await db_session.flush()
    card = Card(name=name, tcg="pokemon", set_id=card_set.id)
    card.card_metadata = (
        {"pricing_summary": {"market": {"amount": market}}}
        if market is not None
        else {}
    )
    db_session.add(card)
    await db_session.flush()
    return card


@pytest.mark.asyncio
async def test_backfill_fills_null_values(db_session, created_user) -> None:
    """A holding stored with no value picks one up from the card's price."""
    from app.models.grade import GradedCard

    user = created_user
    card = await _seed_card(db_session, name="Umbreon VMAX", market="100.00")

    holding = GradedCard(
        user_id=user.id,
        card_id=card.id,
        grade=Decimal("0"),
        house="loupe",
        condition=RawConditionEnum.nm,
        estimated_value_usd=None,
    )
    db_session.add(holding)
    await db_session.commit()

    updated = await hv.backfill_missing_values(db_session)

    assert updated == 1
    await db_session.refresh(holding)
    assert holding.estimated_value_usd == Decimal("100.00")


@pytest.mark.asyncio
async def test_backfill_never_overwrites_an_owner_set_value(
    db_session, created_user
) -> None:
    """Including a deliberate 0 — that's a statement, not a missing value."""
    from app.models.grade import GradedCard

    user = created_user
    card = await _seed_card(db_session, name="Bulk Common", market="100.00")

    priced = GradedCard(
        user_id=user.id,
        card_id=card.id,
        grade=Decimal("0"),
        house="loupe",
        condition=RawConditionEnum.nm,
        estimated_value_usd=Decimal("0"),
    )
    db_session.add(priced)
    await db_session.commit()

    updated = await hv.backfill_missing_values(db_session)

    assert updated == 0
    await db_session.refresh(priced)
    assert priced.estimated_value_usd == Decimal("0")


@pytest.mark.asyncio
async def test_backfill_is_idempotent(db_session, created_user) -> None:
    """Running twice must not double-apply or thrash rows."""
    from app.models.grade import GradedCard

    user = created_user
    card = await _seed_card(db_session, name="Charizard", market="50.00")
    db_session.add(
        GradedCard(
            user_id=user.id,
            card_id=card.id,
            grade=Decimal("0"),
            house="loupe",
            condition=RawConditionEnum.lp,
            estimated_value_usd=None,
        )
    )
    await db_session.commit()

    first = await hv.backfill_missing_values(db_session)
    second = await hv.backfill_missing_values(db_session)

    assert first == 1
    assert second == 0


@pytest.mark.asyncio
async def test_backfill_leaves_unpriced_cards_null_for_a_later_run(
    db_session, created_user
) -> None:
    from app.models.grade import GradedCard

    user = created_user
    card = await _seed_card(db_session, name="No Price Yet", market=None)
    holding = GradedCard(
        user_id=user.id,
        card_id=card.id,
        grade=Decimal("0"),
        house="loupe",
        condition=RawConditionEnum.nm,
        estimated_value_usd=None,
    )
    db_session.add(holding)
    await db_session.commit()

    updated = await hv.backfill_missing_values(db_session)

    assert updated == 0
    await db_session.refresh(holding)
    assert holding.estimated_value_usd is None
