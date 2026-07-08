"""PriceCharting provider — full grade-ladder + metadata extraction.

The single ``/api/product`` call we already make returns a whole per-grade price
ladder plus yearly sales volume and identifiers. These assert we keep all of it
(and degrade gracefully to just the raw price on a limited tier).
"""

from __future__ import annotations

from app.integrations.pricecharting import reduce_product

# A rich product response (prices are integer pennies, per the API).
_FULL = {
    "status": "success",
    "id": "6910",
    "product-name": "Charizard #4",
    "console-name": "Pokemon Base Set",
    "release-date": "1999-01-09",
    "loose-price": 30000,  # $300 raw
    "cib-price": 45000,  # PSA 7
    "new-price": 60000,  # PSA 8
    "graded-price": 90000,  # PSA 9
    "box-only-price": 120000,  # BGS 9.5
    "manual-only-price": 250000,  # PSA 10
    "bgs-10-price": 400000,  # BGS 10
    "condition-17-price": 300000,  # CGC 10
    "condition-18-price": 280000,  # SGC 10
    "sales-volume": 1234,
}


def test_reduce_extracts_full_grade_ladder():
    mp = reduce_product(_FULL)
    assert mp is not None
    # Backward-compatible low/mid/high shape is preserved.
    assert mp.low == 300.0  # loose
    assert mp.mid == 600.0  # new
    assert mp.high == 900.0  # graded (PSA 9)

    ladder = mp.extras["grade_ladder"]
    assert ladder == {
        "UNGRADED": 300.0,
        "PSA 7": 450.0,
        "PSA 8": 600.0,
        "PSA 9": 900.0,
        "BGS 9.5": 1200.0,
        "PSA 10": 2500.0,
        "BGS 10": 4000.0,
        "CGC 10": 3000.0,
        "SGC 10": 2800.0,
    }
    assert mp.extras["sales_volume"] == 1234
    assert mp.extras["pc_id"] == "6910"
    assert mp.extras["release_date"] == "1999-01-09"


def test_reduce_raw_only_tier_still_works():
    """A limited tier that only returns the loose price yields just UNGRADED —
    the richer grades light up automatically when the tier provides them."""
    mp = reduce_product(
        {"status": "success", "product-name": "X", "loose-price": 500}
    )
    assert mp is not None
    assert mp.extras["grade_ladder"] == {"UNGRADED": 5.0}
    assert "sales_volume" not in mp.extras


def test_reduce_none_when_no_prices():
    assert reduce_product({"status": "success"}) is None
    # Zero/garbage prices are dropped, not surfaced as $0.00 grades.
    mp = reduce_product({"loose-price": 0, "graded-price": "n/a"})
    assert mp is None
