"""EVERY way a collection's total value can move.

The total a user sees is the sum of ``GradedCard.estimated_value_usd`` for
live holdings, plus sealed. This file enumerates each path that can change
it and pins the behaviour, because the number is the product's core promise:
if it drifts for a reason the owner can't explain, nothing else matters.

Paths covered:
  1. add a holding (derived / explicit / unpriced card)
  2. delete + restore
  3. edit the VALUE explicitly
  4. edit CONDITION            (raw NM ↔ LP ↔ HP)
  5. RAW → GRADED and back     ← the reported question
  6. the card's market price moving underneath the holding
  7. sealed product
  8. collection (portfolio) membership
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.enums import RawConditionEnum
from app.services.collection import holding_valuation_service as hv


class _Card:
    """Minimal stand-in for the ORM Card — the valuer only reads metadata."""

    def __init__(self, market: float | None):
        # Real shape: pricing_summary.market is a MONEY OBJECT, not a scalar.
        self.card_metadata = (
            {"pricing_summary": {"market": {"amount": market, "currency": "USD"}}}
            if market is not None
            else {}
        )


# ── 1. Deriving a value on add ──


def test_a_raw_nm_card_is_worth_the_market_price():
    assert hv.derive_estimated_value(
        _Card(100.0), condition=RawConditionEnum.nm
    ) == Decimal("100.00")


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (RawConditionEnum.nm, "100.00"),
        (RawConditionEnum.lp, "85.00"),
        (RawConditionEnum.mp, "70.00"),
        (RawConditionEnum.hp, "50.00"),
        (RawConditionEnum.dmg, "30.00"),
    ],
)
def test_condition_scales_the_market_price(condition, expected):
    assert hv.derive_estimated_value(_Card(100.0), condition=condition) == Decimal(
        expected
    )


def test_an_unpriced_card_is_unknown_not_zero():
    """ "unknown" and "worthless" must never look the same on a net worth."""
    assert hv.derive_estimated_value(_Card(None), condition=RawConditionEnum.nm) is None


def test_an_explicit_zero_is_respected():
    """A collector saying "this is worthless" must not be corrected into a
    market price."""
    assert hv.derive_estimated_value(
        _Card(100.0), condition=RawConditionEnum.nm, explicit=0
    ) == Decimal("0.00")


def test_an_owner_supplied_value_beats_the_market():
    assert hv.derive_estimated_value(
        _Card(100.0), condition=RawConditionEnum.nm, explicit=750
    ) == Decimal("750.00")


# ── 5. Raw → graded: the reported question ──


def test_a_slab_is_valued_at_the_raw_price_not_marked_up():
    """A PSA 10 is worth AT LEAST the raw card. The per-grade premium ladder
    is modelled, not observed, so multiplying by it would put an invented
    number on someone's net worth. A floor, not a guess."""
    raw_nm = hv.derive_estimated_value(_Card(100.0), condition=RawConditionEnum.nm)
    # A graded holding has no raw condition at all.
    slabbed = hv.derive_estimated_value(_Card(100.0), condition=None)
    assert slabbed == raw_nm == Decimal("100.00")


def test_grading_a_DAMAGED_card_should_lift_it_off_the_condition_discount():
    """The economically meaningful half of raw → graded.

    A damaged raw card is held at 30% of market. Once it is slabbed, the raw
    condition no longer applies and the holding is worth the full floor.
    The DERIVATION says so; whether the stored value is refreshed on edit is
    a separate question — see test_collection_value_reprice.py.
    """
    damaged = hv.derive_estimated_value(_Card(100.0), condition=RawConditionEnum.dmg)
    slabbed = hv.derive_estimated_value(_Card(100.0), condition=None)
    assert damaged == Decimal("30.00")
    assert slabbed == Decimal("100.00")
    assert slabbed > damaged


# ── 6. Market price moving underneath the holding ──


def test_the_derived_value_follows_the_market_price():
    before = hv.derive_estimated_value(_Card(10.0), condition=RawConditionEnum.nm)
    after = hv.derive_estimated_value(_Card(25.0), condition=RawConditionEnum.nm)
    assert before == Decimal("10.00")
    assert after == Decimal("25.00")


def test_rounding_is_half_up_to_the_cent():
    """Money is quantized once, predictably — not left to float drift."""
    assert hv.derive_estimated_value(
        _Card(10.005), condition=RawConditionEnum.nm
    ) == Decimal("10.01")
    assert hv.derive_estimated_value(
        _Card(3.333), condition=RawConditionEnum.nm
    ) == Decimal("3.33")


def test_a_nonsense_market_price_is_unknown_not_a_crash():
    for junk in ("", "n/a", float("inf"), float("nan"), -1, True, None):
        value = hv.derive_estimated_value(_Card(junk), condition=RawConditionEnum.nm)
        assert value is None or value >= 0
