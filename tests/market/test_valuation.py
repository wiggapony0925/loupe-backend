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
async def test_valuation_none_for_unknown_card(monkeypatch):
    async def no_card(card_id: str) -> None:
        return None

    monkeypatch.setattr(vs.card_search_service, "get_card", no_card)
    assert await vs.get_valuation("nope:404") is None
