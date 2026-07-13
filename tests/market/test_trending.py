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
        # A real price so the card survives the shelf's priced-only filter —
        # `/v1/cards/trending` now drops "—" tiles (matching the web storefront).
        "cardmarket": {"prices": {"averageSellPrice": 10.0 + idx}},
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
        "prices": {"usd": f"{20.0 + idx:.2f}"},
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
        "card_prices": [{"tcgplayer_price": f"{30.0 + idx:.2f}"}],
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
    # The trending pool now falls back to each game's value source, so patch
    # those too — "all providers down" must take the whole chain down.
    monkeypatch.setattr(trending_service, "_valuable_pokemon", _pokemon)
    monkeypatch.setattr(trending_service, "_valuable_magic", _magic)
    monkeypatch.setattr(trending_service, "_valuable_yugioh", _yugi)


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
    # All upstreams down → the resilience path yields only unpriced fallback
    # stubs, which the shelf filters out (no "—" tiles in a shopping rail). The
    # endpoint still degrades to 200 with an empty rail rather than a 5xx.
    assert body["source"] == "fallback"
    assert body["cards"] == []


def test_rotate_window_is_deterministic_permutation():
    pool = [{"id": str(i)} for i in range(20)]
    once = trending_service._rotate_window(pool, salt=1)
    twice = trending_service._rotate_window(pool, salt=1)
    # Same window + same salt → identical order (stable while browsing).
    assert once == twice
    # It's a permutation — every card is still present, none dropped/dupes.
    assert sorted(c["id"] for c in once) == sorted(c["id"] for c in pool)
    # Different salt → different order (providers don't rotate in lockstep).
    assert trending_service._rotate_window(pool, salt=2) != once


def test_rotation_stamp_is_half_day():
    """The stamp rolls at midnight AND noon UTC — YYYYMMDD.AM / YYYYMMDD.PM —
    so the trending rotation refreshes twice a day instead of once."""
    stamp = trending_service._rotation_stamp()
    date_part, _, half = stamp.partition(".")
    assert len(date_part) == 8 and date_part.isdigit()
    assert half in ("AM", "PM")


def test_distinct_by_name_drops_duplicate_printings():
    cards = [
        {"id": "a", "name": "Mega Greninja ex"},
        {"id": "b", "name": "mega greninja EX"},  # alt printing, same name
        {"id": "c", "name": "Cinccino ex"},
        {"id": "d", "name": ""},  # no name → kept (can't dedupe)
    ]
    out = trending_service._distinct_by_name(cards)
    names = [c["id"] for c in out]
    assert names == ["a", "c", "d"]  # second "greninja" dropped


def test_price_of_falls_through_bands_and_handles_unpriced():
    money = lambda amt: {"amount": amt, "currency": "USD"}  # noqa: E731
    assert (
        trending_service._price_of({"pricing_summary": {"market": money(12.5)}}) == 12.5
    )
    # No market → fall through to high.
    assert trending_service._price_of({"pricing_summary": {"high": money(8.0)}}) == 8.0
    # No price at all → None (so it can be filtered out).
    assert trending_service._price_of({"pricing_summary": None}) is None
    assert trending_service._price_of({}) is None


def test_priced_arted_drops_unpriced_and_sorts_desc():
    money = lambda amt: {"amount": amt, "currency": "USD"}  # noqa: E731
    cards = [
        {
            "name": "Cheap",
            "image_url": "a.png",
            "pricing_summary": {"market": money(2)},
        },
        {
            "name": "Rich",
            "image_url": "b.png",
            "pricing_summary": {"market": money(99)},
        },
        {"name": "NoPrice", "image_url": "c.png", "pricing_summary": None},
        {
            "name": "NoArt",
            "images": None,
            "image_url": None,
            "pricing_summary": {"market": money(50)},
        },
    ]
    out = trending_service._priced_arted(cards)
    assert [c["name"] for c in out] == [
        "Rich",
        "Cheap",
    ]  # priciest first, unpriced/art-less gone


def _priced(id_: str, name: str, price: float) -> dict[str, Any]:
    return {
        "id": id_,
        "name": name,
        "tcg": "pokemon",
        "image_url": f"https://example.test/{id_}.png",
        "pricing_summary": {"market": {"amount": price, "currency": "USD"}},
    }


@pytest.mark.asyncio
async def test_trending_shelf_value_differs_priced_and_capped(client, monkeypatch):
    """The mobile endpoint honours `sort` + `max_price` and only ships priced
    cards — so `value` ≠ `trending`, no "—" tiles, and the "steals" cut works."""

    async def fake_trending(tcg: str = "all", limit: int = 24) -> dict[str, Any]:
        return {
            "cards": [
                _priced("t1", "Trend One", 8.0),
                _priced("t2", "Trend Two", 40.0),
            ],
            "source": "live",
        }

    async def fake_valuable(tcg: str = "all", limit: int = 24) -> dict[str, Any]:
        return {
            "cards": [
                _priced("v1", "Value One", 500.0),
                _priced("v2", "Value Two", 5.0),
            ],
            "source": "live",
        }

    monkeypatch.setattr(trending_service, "get_trending", fake_trending)
    monkeypatch.setattr(trending_service, "get_most_valuable", fake_valuable)

    trending = (await client.get("/v1/cards/trending?sort=trending")).json()["data"]
    value = (await client.get("/v1/cards/trending?sort=value")).json()["data"]

    trending_ids = [c["id"] for c in trending["cards"]]
    value_ids = [c["id"] for c in value["cards"]]
    # Distinct sources → the two rails are not the same cards.
    assert set(trending_ids).isdisjoint(value_ids)
    # `value` draws the most-valuable pool, priciest first.
    assert value_ids == ["v1", "v2"]
    # Every card on either rail is priced (no "—" tile that the detail prices).
    for c in trending["cards"] + value["cards"]:
        assert (c.get("pricing_summary") or {}).get("market", {}).get(
            "amount"
        ) is not None
    # The "steals under $X" cut is applied server-side.
    cheap = (await client.get("/v1/cards/trending?sort=value&max_price=10")).json()[
        "data"
    ]
    assert [c["id"] for c in cheap["cards"]] == ["v2"]


@pytest.mark.asyncio
async def test_trending_rejects_invalid_tcg(client):
    # lorcana has no trending feed and isn't in the endpoint's tcg set.
    resp = await client.get("/v1/cards/trending?tcg=lorcana")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_trending_catalog_only_game_is_empty(client):
    # One Piece / Digimon have no price feed → an empty priced rail (200), not
    # a 422 and not a mismatched Magic-dominated fallback.
    resp = await client.get("/v1/cards/trending?tcg=onepiece&sort=value")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["cards"] == []


@pytest.mark.asyncio
async def test_trending_rejects_invalid_limit(client):
    resp = await client.get("/v1/cards/trending?limit=999")
    assert resp.status_code == 422
