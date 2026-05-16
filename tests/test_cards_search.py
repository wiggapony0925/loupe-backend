"""Card catalog tests — legacy DB search + live upstream proxy."""

from __future__ import annotations

from typing import Any

import pytest

from app.models.card import Card, CardSet
from app.models.enums import TcgEnum
from app.services import card_search_service


@pytest.mark.asyncio
async def test_search_cards_returns_pagination(client, db_session):
    cset = CardSet(tcg=TcgEnum.pokemon, name="Base Set", code="BASE")
    db_session.add(cset)
    await db_session.flush()
    db_session.add(Card(set_id=cset.id, tcg=TcgEnum.pokemon, name="Charizard"))
    db_session.add(Card(set_id=cset.id, tcg=TcgEnum.pokemon, name="Pikachu"))
    await db_session.commit()

    resp = await client.get("/v1/cards", params={"q": "char", "tcg": "pokemon"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    assert any(item["name"].lower().startswith("char") for item in body["items"])


# ---------------------------------------------------------------------- live


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    """No-op cache for live-search tests so monkeypatched upstreams are hit."""

    async def _noop_get(_key: str) -> None:
        return None

    async def _noop_set(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(card_search_service, "_cache_get", _noop_get)
    monkeypatch.setattr(card_search_service, "_cache_set", _noop_set)


@pytest.mark.asyncio
async def test_live_search_pokemon(client, monkeypatch):
    async def fake_search(
        query: str, page: int = 1, page_size: int = 25
    ) -> dict[str, Any]:
        assert "Charizard" in query
        return {
            "data": [
                {
                    "id": "base1-4",
                    "name": "Charizard",
                    "number": "4",
                    "rarity": "Holo Rare",
                    "set": {
                        "id": "base1",
                        "name": "Base Set",
                        "releaseDate": "1999/01/09",
                    },
                    "images": {"small": "https://img/4.png"},
                }
            ],
            "totalCount": 1,
        }

    monkeypatch.setattr(card_search_service.pokemon_tcg, "search_cards", fake_search)

    resp = await client.get(
        "/v1/cards/search", params={"q": "Charizard", "tcg": "pokemon", "limit": 5}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "pokemontcg"
    assert body["total"] == 1
    assert len(body["results"]) == 1
    card = body["results"][0]
    assert card["id"] == "pokemontcg:base1-4"
    assert card["name"] == "Charizard"
    assert card["tcg"] == "pokemon"
    assert card["set_name"] == "Base Set"
    assert card["set_code"] == "base1"
    assert card["image_url"] == "https://img/4.png"
    assert card["year"] == 1999
    assert card["source"] == "pokemontcg"


@pytest.mark.asyncio
async def test_live_search_magic(client, monkeypatch):
    async def fake_search(query: str, page: int = 1) -> dict[str, Any]:
        return {
            "data": [
                {
                    "id": "abc-123",
                    "name": "Black Lotus",
                    "set": "lea",
                    "set_name": "Limited Edition Alpha",
                    "collector_number": "232",
                    "rarity": "rare",
                    "released_at": "1993-08-05",
                    "image_uris": {"normal": "https://img/lotus.png"},
                }
            ],
            "total_cards": 1,
        }

    monkeypatch.setattr(card_search_service.scryfall, "search_cards", fake_search)
    resp = await client.get("/v1/cards/search", params={"q": "lotus", "tcg": "magic"})
    body = resp.json()
    assert body["source"] == "scryfall"
    assert body["results"][0]["id"] == "scryfall:abc-123"
    assert body["results"][0]["tcg"] == "magic"
    assert body["results"][0]["year"] == 1993


@pytest.mark.asyncio
async def test_live_search_yugioh(client, monkeypatch):
    async def fake_search(query: str) -> dict[str, Any]:
        return {
            "data": [
                {
                    "id": 89631139,
                    "name": "Blue-Eyes White Dragon",
                    "card_images": [
                        {
                            "image_url": "https://img/beyes.png",
                            "image_url_small": "https://img/beyes-s.png",
                        }
                    ],
                    "card_sets": [
                        {
                            "set_name": "Legend of Blue Eyes",
                            "set_code": "LOB-001",
                            "set_rarity": "Ultra Rare",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(card_search_service.ygoprodeck, "search_cards", fake_search)
    resp = await client.get("/v1/cards/search", params={"q": "Blue", "tcg": "yugioh"})
    body = resp.json()
    assert body["source"] == "ygoprodeck"
    assert body["results"][0]["id"] == "ygoprodeck:89631139"
    assert body["results"][0]["set_code"] == "LOB-001"


@pytest.mark.asyncio
async def test_live_search_empty_query_returns_empty(client):
    resp = await client.get("/v1/cards/search", params={"q": "", "tcg": "pokemon"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"results": [], "total": 0, "source": "pokemontcg"}


@pytest.mark.asyncio
async def test_live_search_upstream_error_graceful(client, monkeypatch):
    import httpx

    async def boom(*_a, **_kw):
        raise httpx.ConnectError("dns failure")

    monkeypatch.setattr(card_search_service.scryfall, "search_cards", boom)
    resp = await client.get("/v1/cards/search", params={"q": "anything", "tcg": "all"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert body["total"] == 0
    assert body["source"] == "scryfall"
    assert "error" in body


@pytest.mark.asyncio
async def test_get_card_by_composite_id(client, monkeypatch):
    async def fake_get(card_id: str) -> dict[str, Any]:
        assert card_id == "base1-4"
        return {
            "id": "base1-4",
            "name": "Charizard",
            "set": {"id": "base1", "name": "Base Set", "releaseDate": "1999/01/09"},
            "images": {"small": "https://img/4.png"},
            "rarity": "Holo Rare",
            "number": "4",
        }

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake_get)
    resp = await client.get("/v1/cards/pokemontcg:base1-4")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "pokemontcg:base1-4"
    assert body["tcg"] == "pokemon"


@pytest.mark.asyncio
async def test_get_card_unknown_composite_id_404(client, monkeypatch):
    async def fake_get(_card_id: str) -> None:
        return None

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake_get)
    resp = await client.get("/v1/cards/pokemontcg:does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_sets_live_pokemon(client, monkeypatch):
    async def fake_list() -> list[dict[str, Any]]:
        return [
            {
                "id": "base1",
                "name": "Base Set",
                "releaseDate": "1999/01/09",
                "total": 102,
                "images": {"logo": "https://img/base1.png"},
            }
        ]

    monkeypatch.setattr(card_search_service.pokemon_tcg, "list_sets", fake_list)
    resp = await client.get("/v1/sets", params={"tcg": "pokemon"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "pokemontcg"
    assert body["total"] == 1
    assert body["results"][0]["code"] == "base1"


@pytest.mark.asyncio
async def test_list_sets_live_magic(client, monkeypatch):
    async def fake_list() -> list[dict[str, Any]]:
        return [
            {
                "id": "abc",
                "code": "lea",
                "name": "Limited Edition Alpha",
                "released_at": "1993-08-05",
                "card_count": 295,
            }
        ]

    monkeypatch.setattr(card_search_service.scryfall, "list_sets", fake_list)
    resp = await client.get("/v1/sets", params={"tcg": "magic"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "scryfall"
    assert body["results"][0]["code"] == "lea"


@pytest.mark.asyncio
async def test_endpoints_are_public_no_auth_required(client):
    """No bearer token attached — must still be 200."""
    resp = await client.get("/v1/cards/search", params={"q": "", "tcg": "all"})
    assert resp.status_code == 200
    assert "WWW-Authenticate" not in resp.headers
