"""Card catalog tests — legacy DB search + live upstream proxy."""

from __future__ import annotations

from typing import Any

import pytest

from app.models.card import Card, CardSet
from app.models.enums import TcgEnum
from app.services.catalog import card_search_service
from tests.conftest import (
    assert_envelope_error,
    assert_envelope_ok,
    envelope_pagination,
)


@pytest.mark.asyncio
async def test_search_cards_returns_pagination(client, db_session):
    cset = CardSet(tcg=TcgEnum.pokemon, name="Base Set", code="BASE")
    db_session.add(cset)
    await db_session.flush()
    db_session.add(Card(set_id=cset.id, tcg=TcgEnum.pokemon, name="Charizard"))
    db_session.add(Card(set_id=cset.id, tcg=TcgEnum.pokemon, name="Pikachu"))
    await db_session.commit()

    resp = await client.get("/v1/cards", params={"q": "char", "tcg": "pokemon"})
    items = assert_envelope_ok(resp)
    pagination = envelope_pagination(resp)
    assert pagination["total"] >= 1
    assert any(item["name"].lower().startswith("char") for item in items)


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
        assert "charizard" in query.lower()
        # Wildcard MUST live outside the quoted phrase or the Pokémon TCG
        # API treats `*` as a literal character and returns no matches.
        assert '"' not in query
        assert query.endswith("*")
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
    body = assert_envelope_ok(resp)
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
    body = assert_envelope_ok(resp)
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
    body = assert_envelope_ok(resp)
    assert body["source"] == "ygoprodeck"
    assert body["results"][0]["id"] == "ygoprodeck:89631139"
    assert body["results"][0]["set_code"] == "LOB-001"


@pytest.mark.asyncio
async def test_live_search_empty_query_returns_empty(client):
    resp = await client.get("/v1/cards/search", params={"q": "", "tcg": "pokemon"})
    body = assert_envelope_ok(resp)
    assert body["results"] == []
    assert body["total"] == 0
    assert body["source"] == "pokemontcg"


@pytest.mark.asyncio
async def test_live_search_upstream_error_graceful(client, monkeypatch):
    """When every upstream blows up, return an empty success envelope."""
    import httpx

    async def boom(*_a, **_kw):
        raise httpx.ConnectError("dns failure")

    monkeypatch.setattr(card_search_service.scryfall, "search_cards", boom)
    monkeypatch.setattr(card_search_service.pokemon_tcg, "search_cards", boom)
    monkeypatch.setattr(card_search_service.ygoprodeck, "search_cards", boom)
    resp = await client.get(
        "/v1/cards/search", params={"q": "anything", "tcg": "magic"}
    )
    body = assert_envelope_ok(resp)
    assert body["results"] == []
    assert body["total"] == 0
    assert body["source"] == "scryfall"
    # Upstream-degradation flag is a *data*-level field (200 success envelope).
    assert "error" in body


@pytest.mark.asyncio
async def test_live_search_all_ranks_by_relevance(client, monkeypatch):
    """tcg=all fans out to every provider, then ranks the pool by relevance
    to the query (best match first), keeping all providers represented."""

    async def fake_pokemon(query: str, page: int = 1, page_size: int = 25):
        return {
            "data": [
                {
                    "id": f"base1-{i}",
                    "name": f"Poke{i}",
                    "set": {
                        "id": "base1",
                        "name": "Base Set",
                        "releaseDate": "1999/01/09",
                    },
                    "images": {"small": f"https://img/p{i}.png"},
                    "rarity": "Common",
                }
                for i in range(3)
            ]
        }

    async def fake_scryfall(query: str, page: int = 1):
        return {
            "data": [
                {
                    "id": f"mtg-{i}",
                    "name": f"Magic{i}",
                    "set": "lea",
                    "set_name": "Alpha",
                    "collector_number": str(i),
                    "rarity": "rare",
                    "released_at": "1993-08-05",
                    "image_uris": {"normal": f"https://img/m{i}.png"},
                    "prices": {"usd": "10.00"},
                }
                for i in range(3)
            ]
        }

    async def fake_yugi(query: str):
        return {
            "data": [
                {
                    "id": 1000 + i,
                    "name": f"Ygo{i}",
                    "card_images": [{"image_url": f"https://img/y{i}.png"}],
                    "card_sets": [{"set_name": "LOB", "set_code": f"LOB-{i:03d}"}],
                }
                for i in range(3)
            ]
        }

    monkeypatch.setattr(card_search_service.pokemon_tcg, "search_cards", fake_pokemon)
    monkeypatch.setattr(card_search_service.scryfall, "search_cards", fake_scryfall)
    monkeypatch.setattr(card_search_service.ygoprodeck, "search_cards", fake_yugi)

    # Query the exact name of one provider's card — it must rank #1.
    resp = await client.get(
        "/v1/cards/search", params={"q": "Magic1", "tcg": "all", "limit": 9}
    )
    body = assert_envelope_ok(resp)
    assert body["source"] == "mixed"
    assert len(body["results"]) == 9
    # Relevance ranking: the exact match leads regardless of provider order.
    assert body["results"][0]["name"] == "Magic1"
    # All three providers are still represented in the pool.
    assert {r["tcg"] for r in body["results"]} == {"pokemon", "magic", "yugioh"}


@pytest.mark.asyncio
async def test_provider_not_configured(client):
    """Games with no catalog provider (Lorcana / Sports) return an empty
    success envelope. (One Piece / Digimon are now data-backed.)"""
    for tcg in ("lorcana", "sports"):
        resp = await client.get("/v1/cards/search", params={"q": "x", "tcg": tcg})
        body = assert_envelope_ok(resp)
        assert body["results"] == []
        assert body["error"] == "provider_not_configured"


@pytest.mark.asyncio
async def test_rich_attributes_pokemon(client, monkeypatch):
    async def fake(*_a, **_kw):
        return {
            "data": [
                {
                    "id": "base1-4",
                    "name": "Charizard",
                    "supertype": "Pokémon",
                    "subtypes": ["Stage 2"],
                    "hp": "120",
                    "types": ["Fire"],
                    "attacks": [{"name": "Fire Spin", "damage": "100"}],
                    "weaknesses": [{"type": "Water", "value": "x2"}],
                    "set": {
                        "id": "base1",
                        "name": "Base Set",
                        "series": "Base",
                        "releaseDate": "1999/01/09",
                        "total": 102,
                        "printedTotal": 102,
                        "images": {
                            "logo": "https://img/base1-logo.png",
                            "symbol": "https://img/base1-sym.png",
                        },
                    },
                    "images": {
                        "small": "https://img/4s.png",
                        "large": "https://img/4l.png",
                    },
                    "rarity": "Rare Holo",
                    "number": "4",
                    "artist": "Mitsuhiro Arita",
                    "tcgplayer": {
                        "url": "https://tcgplayer.com/x",
                        "updatedAt": "2024-01-01",
                        "prices": {
                            "holofoil": {
                                "low": 100.0,
                                "mid": 200.0,
                                "high": 500.0,
                                "market": 250.0,
                            }
                        },
                    },
                }
            ]
        }

    monkeypatch.setattr(card_search_service.pokemon_tcg, "search_cards", fake)
    resp = await client.get("/v1/cards/search", params={"q": "char", "tcg": "pokemon"})
    body = assert_envelope_ok(resp)
    card = body["results"][0]
    attrs = card["attributes"]
    assert attrs["hp"] == "120"
    assert attrs["supertype"] == "Pokémon"
    assert attrs["types"] == ["Fire"]
    assert attrs["attacks"][0]["name"] == "Fire Spin"
    assert attrs["artist"] == "Mitsuhiro Arita"
    assert "tcgplayer_url" in attrs
    assert card["pricing_summary"]["market"]["amount"] == 250.0
    assert card["pricing_summary"]["low"]["amount"] == 100.0
    assert card["set"]["printed_total"] == 102
    assert card["set"]["logo"]["url"] == "https://img/base1-logo.png"
    assert card["images"]["large"]["url"] == "https://img/4l.png"
    assert "vintage" in card["tags"]
    assert card["metadata"]["source"] == "pokemontcg"
    assert card["metadata"]["confidence"] == 1.0


@pytest.mark.asyncio
async def test_rich_attributes_magic(client, monkeypatch):
    async def fake(*_a, **_kw):
        return {
            "data": [
                {
                    "id": "abc",
                    "name": "Black Lotus",
                    "mana_cost": "{0}",
                    "cmc": 0,
                    "type_line": "Artifact",
                    "oracle_text": "{T}, Sacrifice ~",
                    "colors": [],
                    "color_identity": [],
                    "keywords": [],
                    "legalities": {"vintage": "restricted"},
                    "reserved": True,
                    "set": "lea",
                    "set_name": "Alpha",
                    "collector_number": "232",
                    "rarity": "rare",
                    "released_at": "1993-08-05",
                    "image_uris": {
                        "small": "https://img/s.png",
                        "normal": "https://img/n.png",
                        "large": "https://img/l.png",
                        "art_crop": "https://img/a.png",
                    },
                    "prices": {"usd": "50000.00"},
                    "artist": "Christopher Rush",
                }
            ]
        }

    monkeypatch.setattr(card_search_service.scryfall, "search_cards", fake)
    resp = await client.get("/v1/cards/search", params={"q": "lotus", "tcg": "magic"})
    card = assert_envelope_ok(resp)["results"][0]
    assert card["attributes"]["type_line"] == "Artifact"
    assert card["attributes"]["reserved"] is True
    assert card["attributes"]["legalities"]["vintage"] == "restricted"
    assert card["images"]["art_crop"]["url"] == "https://img/a.png"
    assert card["pricing_summary"]["market"]["amount"] == 50000.0
    assert "reserved" in card["tags"]
    assert "vintage" in card["tags"]


@pytest.mark.asyncio
async def test_rich_attributes_yugioh(client, monkeypatch):
    async def fake(*_a, **_kw):
        return {
            "data": [
                {
                    "id": 89631139,
                    "name": "Blue-Eyes White Dragon",
                    "type": "Normal Monster",
                    "frameType": "normal",
                    "desc": "This legendary dragon...",
                    "race": "Dragon",
                    "atk": 3000,
                    "def": 2500,
                    "level": 8,
                    "attribute": "LIGHT",
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
                    "card_prices": [
                        {
                            "tcgplayer_price": "5.00",
                            "cardmarket_price": "4.50",
                            "ebay_price": "6.00",
                            "amazon_price": "0",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(card_search_service.ygoprodeck, "search_cards", fake)
    resp = await client.get("/v1/cards/search", params={"q": "blue", "tcg": "yugioh"})
    card = assert_envelope_ok(resp)["results"][0]
    assert card["attributes"]["atk"] == 3000
    assert card["attributes"]["def"] == 2500
    assert card["attributes"]["attribute"] == "LIGHT"
    assert card["pricing_summary"]["market"]["amount"] > 0
    assert card["pricing_summary"]["sample_size"] == 3
    assert card["images"]["normal"]["url"] == "https://img/beyes.png"


@pytest.mark.asyncio
async def test_price_history_endpoint(client, monkeypatch):
    async def fake_get(_card_id: str):
        return {
            "id": "base1-4",
            "name": "Charizard",
            "set": {"id": "base1", "name": "Base Set", "releaseDate": "1999/01/09"},
            "images": {"small": "https://img/4.png"},
            "rarity": "Rare Holo",
            "number": "4",
            "tcgplayer": {
                "updatedAt": "2024-01-01",
                "prices": {
                    "holofoil": {"market": 250.0, "low": 100, "high": 500, "mid": 200}
                },
            },
        }

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake_get)
    resp = await client.get(
        "/v1/cards/pokemontcg:base1-4/prices", params={"range": "30d"}
    )
    body = assert_envelope_ok(resp)
    assert body["card_id"] == "pokemontcg:base1-4"
    assert body["currency"] == "USD"
    assert body["granularity"] == "daily"
    assert len(body["points"]) == 30
    for p in body["points"]:
        assert "ts" in p and "price" in p and "currency" in p
    assert body["summary"]["current"] == 250.0
    assert body["summary"]["n_points"] == 30


@pytest.mark.asyncio
async def test_price_history_unknown_404(client, monkeypatch):
    async def fake_get(_card_id: str) -> None:
        return None

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake_get)
    resp = await client.get("/v1/cards/pokemontcg:nope/prices", params={"range": "30d"})
    assert_envelope_error(resp, expected_status=404)


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
    body = assert_envelope_ok(resp)
    assert body["id"] == "pokemontcg:base1-4"
    assert body["tcg"] == "pokemon"


@pytest.mark.asyncio
async def test_get_card_unknown_composite_id_404(client, monkeypatch):
    async def fake_get(_card_id: str) -> None:
        return None

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake_get)
    resp = await client.get("/v1/cards/pokemontcg:does-not-exist")
    assert_envelope_error(resp, expected_status=404)


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
    body = assert_envelope_ok(resp)
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
    body = assert_envelope_ok(resp)
    assert body["source"] == "scryfall"
    assert body["results"][0]["code"] == "lea"


@pytest.mark.asyncio
async def test_endpoints_are_public_no_auth_required(client):
    """No bearer token attached — must still be 200."""
    resp = await client.get("/v1/cards/search", params={"q": "", "tcg": "all"})
    assert_envelope_ok(resp)
    assert "WWW-Authenticate" not in resp.headers


@pytest.mark.asyncio
async def test_precise_search_pins_collector_number(monkeypatch):
    """A clean name + number query targets the exact printing."""
    seen: list[str] = []

    async def fake_search(
        query: str, page: int = 1, page_size: int = 25
    ) -> dict[str, Any]:
        seen.append(query)
        assert "number:58" in query
        assert "name:pikachu*" in query
        return {
            "data": [
                {
                    "id": "base1-58",
                    "name": "Pikachu",
                    "number": "58",
                    "set": {
                        "id": "base1",
                        "name": "Base Set",
                        "releaseDate": "1999/01/09",
                    },
                    "images": {"small": "https://img/58.png"},
                }
            ]
        }

    monkeypatch.setattr(card_search_service.pokemon_tcg, "search_cards", fake_search)
    out = await card_search_service.search_cards_precise(
        tcg="pokemon", name="Pikachu", number="58/102"
    )
    assert [c["id"] for c in out] == ["pokemontcg:base1-58"]
    # Name+number matched on the first call — no fallback fan-out.
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_precise_search_falls_back_to_number_only(monkeypatch):
    """When OCR garbles the name so the name query whiffs, the bare
    collector number still recovers the candidate pool."""
    queries: list[str] = []

    async def fake_search(
        query: str, page: int = 1, page_size: int = 25
    ) -> dict[str, Any]:
        queries.append(query)
        if "name:" in query and "number:" in query:
            # Garbled name wildcard matches nothing upstream.
            return {"data": []}
        # Number-only fallback returns every printing with that number.
        assert query == "number:6"
        return {
            "data": [
                {
                    "id": "base1-6",
                    "name": "Gyarados",
                    "number": "6",
                    "set": {
                        "id": "base1",
                        "name": "Base Set",
                        "releaseDate": "1999/01/09",
                    },
                },
                {
                    "id": "ru1-6",
                    "name": "Gyarados",
                    "number": "6",
                    "set": {
                        "id": "ru1",
                        "name": "Pokémon Rumble",
                        "releaseDate": "2009/12/02",
                    },
                },
            ]
        }

    monkeypatch.setattr(card_search_service.pokemon_tcg, "search_cards", fake_search)
    out = await card_search_service.search_cards_precise(
        tcg="pokemon", name="Gyarado5", number="6/102"
    )
    ids = {c["id"] for c in out}
    assert ids == {"pokemontcg:base1-6", "pokemontcg:ru1-6"}
    # First the name+number attempt, then the number-only rescue.
    assert len(queries) == 2
