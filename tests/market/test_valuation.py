"""Card valuation / equilibrium fair-value service."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.market import valuation_service as vs


def _money(amount: float) -> dict[str, Any]:
    return {"amount": amount, "currency": "USD"}


@pytest.mark.asyncio
async def test_fair_value_blends_present_signals(monkeypatch):
    async def fake_card(card_id: str) -> dict[str, Any]:
        return {"id": card_id, "pricing_summary": {"market": _money(100.0)}}

    async def fake_marketplace(card_id: str, **_: Any) -> dict[str, Any]:
        return {"providers": [{"price": _money(80.0)}, {"price": _money(120.0)}]}

    async def fake_grades(card_id: str, **_: Any) -> dict[str, Any]:
        return {
            "grades": [
                {"grade": "UNGRADED", "median_recent": 90.0, "last_sale": _money(92.0)},
                {"grade": "PSA 10", "last_sale": _money(500.0)},
            ]
        }

    monkeypatch.setattr(vs.card_search_service, "get_card", fake_card)
    monkeypatch.setattr(
        vs.marketplace_prices_service,
        "get_marketplace_prices_for_card",
        fake_marketplace,
    )
    monkeypatch.setattr(
        vs.grade_summary_service, "get_grade_summary_for_card", fake_grades
    )

    v = await vs.get_valuation("x:1")
    assert v is not None
    # comps 90 (w .5) + listings median 100 (w .3) + catalog 100 (w .2) = 95
    assert v["fair_value"]["amount"] == 95.0
    assert v["confidence"] == 3
    assert v["signals"]["sold_comps"]["amount"] == 90.0
    assert v["signals"]["listings"]["amount"] == 100.0
    assert len(v["grades"]) == 2


@pytest.mark.asyncio
async def test_fair_value_with_only_catalog(monkeypatch):
    async def fake_card(card_id: str) -> dict[str, Any]:
        return {"id": card_id, "pricing_summary": {"market": _money(42.0)}}

    async def empty_marketplace(card_id: str, **_: Any) -> dict[str, Any]:
        return {"providers": []}

    async def empty_grades(card_id: str, **_: Any) -> None:
        return None

    monkeypatch.setattr(vs.card_search_service, "get_card", fake_card)
    monkeypatch.setattr(
        vs.marketplace_prices_service,
        "get_marketplace_prices_for_card",
        empty_marketplace,
    )
    monkeypatch.setattr(
        vs.grade_summary_service, "get_grade_summary_for_card", empty_grades
    )

    v = await vs.get_valuation("x:2")
    assert v is not None
    assert v["fair_value"]["amount"] == 42.0  # falls back to the one signal
    assert v["confidence"] == 1
    assert v["grades"] == []


@pytest.mark.asyncio
async def test_pricecharting_drives_sold_comps_signal(monkeypatch):
    async def fake_card(card_id: str) -> dict[str, Any]:
        return {"id": card_id, "pricing_summary": {"market": _money(30.0)}}

    async def fake_marketplace(card_id: str, **_: Any) -> dict[str, Any]:
        return {
            "providers": [
                {
                    "source": "pricecharting",
                    "label": "PriceCharting",
                    "price": _money(27.5),
                },
                {"source": "scryfall", "label": "Scryfall", "price": _money(31.0)},
            ]
        }

    async def empty_grades(card_id: str, **_: Any) -> None:
        return None

    monkeypatch.setattr(vs.card_search_service, "get_card", fake_card)
    monkeypatch.setattr(
        vs.marketplace_prices_service,
        "get_marketplace_prices_for_card",
        fake_marketplace,
    )
    monkeypatch.setattr(
        vs.grade_summary_service, "get_grade_summary_for_card", empty_grades
    )

    v = await vs.get_valuation("x:3")
    assert v is not None
    # PriceCharting becomes the real "sold comps" signal…
    assert v["signals"]["sold_comps"]["amount"] == 27.5
    # …and is excluded from listings (only Scryfall remains there).
    assert v["signals"]["listings"]["amount"] == 31.0
    assert v["confidence"] == 3


@pytest.mark.asyncio
async def test_valuation_none_for_unknown_card(monkeypatch):
    async def no_card(card_id: str) -> None:
        return None

    monkeypatch.setattr(vs.card_search_service, "get_card", no_card)
    assert await vs.get_valuation("nope:404") is None


@pytest.mark.asyncio
async def test_pricecharting_grade_ladder_merges_into_grades(monkeypatch):
    """PriceCharting's real per-grade ladder fills the price-by-grade row for
    grades sold-comps don't cover, without overwriting a real sale."""

    async def fake_card(card_id: str) -> dict[str, Any]:
        return {"id": card_id, "pricing_summary": {"market": _money(300.0)}}

    async def fake_marketplace(card_id: str, **_: Any) -> dict[str, Any]:
        return {
            "providers": [
                {
                    "source": "pricecharting",
                    "label": "PriceCharting",
                    "price": _money(300.0),
                    "sales_volume": 1234,
                    "grade_ladder": {
                        "UNGRADED": 300.0,
                        "PSA 9": 900.0,
                        "PSA 10": 2500.0,
                        "BGS 10": 4000.0,
                    },
                }
            ]
        }

    async def fake_grades(card_id: str, **_: Any) -> dict[str, Any]:
        # A real PSA 10 sold comp already exists — the guide must NOT clobber it.
        return {
            "grades": [
                {
                    "grade": "PSA 10",
                    "median_recent": 2600.0,
                    "last_sale": _money(2650.0),
                }
            ]
        }

    monkeypatch.setattr(vs.card_search_service, "get_card", fake_card)
    monkeypatch.setattr(
        vs.marketplace_prices_service,
        "get_marketplace_prices_for_card",
        fake_marketplace,
    )
    monkeypatch.setattr(
        vs.grade_summary_service, "get_grade_summary_for_card", fake_grades
    )

    v = await vs.get_valuation("x:9")
    assert v is not None
    assert v["sales_volume"] == 1234
    by_grade = {r["grade"]: r for r in v["grades"]}
    # Real PSA 10 comp preserved (guide did not overwrite it).
    assert by_grade["PSA 10"]["median_recent"] == 2600.0
    assert "is_guide" not in by_grade["PSA 10"]
    # Grades comps didn't cover are filled from the PriceCharting guide.
    assert by_grade["BGS 10"]["median_recent"] == 4000.0
    assert by_grade["BGS 10"]["is_guide"] is True
    assert by_grade["BGS 10"]["house"] == "bgs"
    assert by_grade["PSA 9"]["is_guide"] is True
    # Sorted UNGRADED-first, then by price desc.
    assert v["grades"][0]["grade"] == "UNGRADED"
