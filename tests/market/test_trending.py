"""Tests for the public ``/v1/cards/trending`` endpoint."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from app.services.catalog import card_search_service
from app.services.market import trending_service


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable Redis read/write so tests are deterministic."""

    async def _get(_key: str) -> None:
        return None

    async def _set(_key: str, _value: dict[str, Any], _ttl: int) -> None:
        return None

    monkeypatch.setattr(card_search_service, "_cache_get", _get)
    monkeypatch.setattr(card_search_service, "_cache_set", _set)
    monkeypatch.setattr(trending_service, "_cache_get", _get)
    monkeypatch.setattr(trending_service, "_cache_set", _set)


def _stub_pokemon_card(idx: int) -> dict[str, Any]:
    return {
        "id": f"base1-{idx}",
        "name": f"Stub Pokémon {idx}",
        "rarity": "Rare Holo",
        "set": {"name": "Base Set", "releaseDate": "1999/01/09"},
        "number": str(idx),
        "images": {
            "small": f"https://example.test/poke/{idx}-small.png",
            "large": f"https://example.test/poke/{idx}-large.png",
        },
    }


def _stub_scryfall_card(idx: int) -> dict[str, Any]:
    return {
        "id": f"sf-{idx:08d}-0000-0000-0000-000000000000",
        "name": f"Stub Magic {idx}",
        "set_name": "Alpha",
        "released_at": "1993-08-05",
        "collector_number": str(idx),
        "rarity": "rare",
        "image_uris": {
            "small": f"https://example.test/sf/{idx}-small.jpg",
            "normal": f"https://example.test/sf/{idx}-normal.jpg",
            "large": f"https://example.test/sf/{idx}-large.jpg",
            "art_crop": f"https://example.test/sf/{idx}-art.jpg",
        },
    }


def _stub_yugi_card(idx: int) -> dict[str, Any]:
    return {
        "id": 89631000 + idx,
        "name": f"Stub Yu-Gi-Oh {idx}",
        "type": "Effect Monster",
        "race": "Dragon",
        "card_images": [
            {
                "image_url": f"https://example.test/ygo/{idx}.jpg",
                "image_url_small": f"https://example.test/ygo/{idx}-small.jpg",
                "image_url_cropped": f"https://example.test/ygo/{idx}-art.jpg",
            }
        ],
    }


def _patch_all_providers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pokemon: Iterable[dict[str, Any]] | BaseException,
    magic: Iterable[dict[str, Any]] | BaseException,
    yugioh: Iterable[dict[str, Any]] | BaseException,
) -> None:
    async def _pokemon(_pool: int = 0) -> list[dict[str, Any]]:
        if isinstance(pokemon, BaseException):
            raise pokemon
        return [card_search_service._from_pokemon(c) for c in pokemon]

    async def _magic(_pool: int = 0) -> list[dict[str, Any]]:
        if isinstance(magic, BaseException):
            raise magic
        return [card_search_service._from_scryfall(c) for c in magic]

    async def _yugi(_pool: int = 0) -> list[dict[str, Any]]:
        if isinstance(yugioh, BaseException):
            raise yugioh
        return [card_search_service._from_yugioh(c) for c in yugioh]

    monkeypatch.setattr(trending_service, "_trending_pokemon", _pokemon)
    monkeypatch.setattr(trending_service, "_trending_magic", _magic)
    monkeypatch.setattr(trending_service, "_trending_yugioh", _yugi)


@pytest.mark.asyncio
async def test_trending_returns_envelope(client, monkeypatch):
    _patch_all_providers(
        monkeypatch,
        pokemon=[_stub_pokemon_card(i) for i in range(1, 6)],
        magic=[_stub_scryfall_card(i) for i in range(1, 6)],
        yugioh=[_stub_yugi_card(i) for i in range(1, 6)],
    )
    resp = await client.get("/v1/cards/trending")
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert "cards" in body
    assert isinstance(body["cards"], list)
    assert len(body["cards"]) >= 1
    assert body["source"] in {"live", "cached", "fallback"}
    assert "updated_at" in body
    tcgs = {c.get("tcg") for c in body["cards"]}
    assert len(tcgs) >= 2


@pytest.mark.asyncio
async def test_trending_with_tcg_filter(client, monkeypatch):
    _patch_all_providers(
        monkeypatch,
        pokemon=[_stub_pokemon_card(i) for i in range(1, 8)],
        magic=[_stub_scryfall_card(i) for i in range(1, 8)],
        yugioh=[_stub_yugi_card(i) for i in range(1, 8)],
    )
    resp = await client.get("/v1/cards/trending?tcg=pokemon&limit=4")
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert len(body["cards"]) == 4
    assert {c["tcg"] for c in body["cards"]} == {"pokemon"}


@pytest.mark.asyncio
async def test_trending_limit_respected(client, monkeypatch):
    _patch_all_providers(
        monkeypatch,
        pokemon=[_stub_pokemon_card(i) for i in range(1, 11)],
        magic=[_stub_scryfall_card(i) for i in range(1, 11)],
        yugioh=[_stub_yugi_card(i) for i in range(1, 11)],
    )
    resp = await client.get("/v1/cards/trending?limit=6")
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert len(body["cards"]) == 6


@pytest.mark.asyncio
async def test_trending_fallback_on_all_providers_down(client, monkeypatch):
    err = RuntimeError("upstream down")
    _patch_all_providers(monkeypatch, pokemon=err, magic=err, yugioh=err)

    async def _fake_get_card(card_id: str) -> dict[str, Any]:
        return {
            "id": card_id,
            "name": "Fallback Card",
            "tcg": card_id.split(":", 1)[0],
            "images": None,
            "image_url": None,
            "set_name": "Stub",
            "year": None,
            "number": None,
            "rarity": None,
            "pricing_summary": None,
            "source": "fallback",
            "attributes": None,
        }

    monkeypatch.setattr(card_search_service, "get_card", _fake_get_card)

    resp = await client.get("/v1/cards/trending?limit=3")
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["source"] == "fallback"
    assert len(body["cards"]) == 3
    for c in body["cards"]:
        assert "id" in c and "name" in c


def test_rotate_daily_is_deterministic_permutation():
    pool = [{"id": str(i)} for i in range(20)]
    once = trending_service._rotate_daily(pool, salt=1)
    twice = trending_service._rotate_daily(pool, salt=1)
    # Same day + same salt → identical order (stable while browsing).
    assert once == twice
    # It's a permutation — every card is still present, none dropped/dupes.
    assert sorted(c["id"] for c in once) == sorted(c["id"] for c in pool)
    # Different salt → different order (providers don't rotate in lockstep).
    assert trending_service._rotate_daily(pool, salt=2) != once


@pytest.mark.asyncio
async def test_trending_rejects_invalid_tcg(client):
    resp = await client.get("/v1/cards/trending?tcg=onepiece")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_trending_rejects_invalid_limit(client):
    resp = await client.get("/v1/cards/trending?limit=999")
    assert resp.status_code == 422
