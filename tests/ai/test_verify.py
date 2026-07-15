"""Tests for the shelf-verification pass (services/ai/verify)."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.ai import providers, verify
from app.services.ai.verify import _parse_keep, review_shelf


def _card(id_: str, name: str, set_name: str = "Base") -> dict[str, Any]:
    return {"id": id_, "name": name, "set_name": set_name}


def test_parse_keep_clamps_and_dedupes() -> None:
    assert _parse_keep('{"keep": [2, 0, 2, 9]}', 3) == [2, 0]
    assert _parse_keep('junk {"keep": [1]} trailing', 2) == [1]
    assert _parse_keep('{"keep": []}', 3) == []
    assert _parse_keep("not json", 3) is None
    assert _parse_keep('{"other": [1]}', 3) is None


@pytest.mark.asyncio
async def test_review_reorders_and_trims(db_engine, monkeypatch) -> None:
    async def fake_ask(system: str, user: str, **kwargs: Any) -> str:
        assert "movie promos" in user
        return '{"keep": [1, 2]}'

    monkeypatch.setattr(providers, "ask", fake_ask)
    pool = [
        _card("a", "Mew", "Sword & Shield Promos"),
        _card("b", "Ancient Mew", "Wizards Black Star Promos"),
        _card("c", "Entei", "Wizards Black Star Promos"),
    ]
    shelf, verified = await review_shelf("movie promos", "pokemon", pool)
    assert verified is True
    assert [c["id"] for c in shelf] == ["b", "c"]


@pytest.mark.asyncio
async def test_review_failure_keeps_the_shelf(db_engine, monkeypatch) -> None:
    async def boom(system: str, user: str, **kwargs: Any) -> str:
        raise RuntimeError("model down")

    monkeypatch.setattr(providers, "ask", boom)
    pool = [_card("a", "Mew"), _card("b", "Entei")]
    shelf, verified = await review_shelf("movie promos", "pokemon", pool)
    assert verified is False
    assert shelf == pool


@pytest.mark.asyncio
async def test_review_empty_keep_is_an_honest_miss(db_engine, monkeypatch) -> None:
    async def nothing_fits(system: str, user: str, **kwargs: Any) -> str:
        return '{"keep": []}'

    monkeypatch.setattr(providers, "ask", nothing_fits)
    pool = [_card("a", "Mew"), _card("b", "Entei")]
    shelf, verified = await review_shelf("movie promos", "pokemon", pool)
    assert verified is True
    assert shelf == []  # the reviewer's verdict beats serving junk


@pytest.mark.asyncio
async def test_review_skips_tiny_or_disabled_shelves(db_engine, monkeypatch) -> None:
    called = False

    async def spy(system: str, user: str, **kwargs: Any) -> str:
        nonlocal called
        called = True
        return '{"keep": [0]}'

    monkeypatch.setattr(providers, "ask", spy)
    shelf, verified = await review_shelf("x", None, [_card("a", "Mew")])
    assert not called and not verified and len(shelf) == 1

    monkeypatch.setattr(verify, "VERIFY_ENABLED", False)
    shelf, verified = await review_shelf(
        "x", None, [_card("a", "Mew"), _card("b", "Entei")]
    )
    assert not called and not verified and len(shelf) == 2
