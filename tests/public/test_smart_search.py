"""Smart marketplace search — /v1/public/search with query understanding.

End-to-end through the router: free text like "most recent from evolving
skies" resolves a REAL set and pages its catalog; "newest pokemon" pages the
whole game newest-first; parsed price/sort filters shape the pooled path;
explicit params always beat parsed intent; and plain queries keep the fast
true-pagination path with a null ``interpreted``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.catalog import card_search_service, catalog_browse_service


def _card(
    id_: str,
    name: str | None = None,
    price: float | None = None,
    rarity: str | None = None,
    set_name: str | None = None,
    year: int | None = None,
) -> dict[str, Any]:
    ps = None if price is None else {"market": {"amount": price, "currency": "USD"}}
    return {
        "id": id_,
        "name": name or id_,
        "rarity": rarity,
        "set_name": set_name,
        "year": year,
        "pricing_summary": ps,
    }


@pytest.fixture()
def pokemon_sets(monkeypatch):
    async def fake_list_sets(tcg: str, sort: str = "catalog", limit=None):
        if tcg != "pokemon":
            return {"results": []}
        return {
            "results": [
                {
                    "id": "pokemontcg:swsh7",
                    "name": "Evolving Skies",
                    "tcg": "pokemon",
                    "release_date": "2021/08/27",
                },
            ]
        }

    monkeypatch.setattr(card_search_service, "list_sets", fake_list_sets)


@pytest.mark.asyncio
async def test_set_query_pages_the_real_set(client, monkeypatch, pokemon_sets):
    calls: list[dict[str, Any]] = []

    async def fake_browse(game, page, page_size, sort="name", set_id=None):
        calls.append(
            {
                "game": game,
                "page": page,
                "page_size": page_size,
                "sort": sort,
                "set_id": set_id,
            }
        )
        return {
            "cards": [
                _card(f"c{i}", set_name="Evolving Skies") for i in range(page_size)
            ],
            "total": 237,
            "source": "pokemontcg",
        }

    monkeypatch.setattr(catalog_browse_service, "browse_catalog", fake_browse)

    resp = await client.get(
        "/v1/public/search",
        params={"q": "most recent from evolving skies", "page": 2, "page_size": 24},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # The parsed set resolved to the REAL set and we paged its catalog.
    assert calls == [
        {
            "game": "pokemon",
            "page": 2,
            "page_size": 24,
            "sort": "newest",
            "set_id": "pokemontcg:swsh7",
        }
    ]
    assert data["total"] == 237  # the set's true card count
    assert len(data["results"]) == 24
    chips = data["interpreted"]["chips"]
    assert "Newest first" in chips
    assert "Set: Evolving Skies" in chips
    assert data["interpreted"]["set_id"] == "pokemontcg:swsh7"


@pytest.mark.asyncio
async def test_newest_game_query_pages_whole_catalog(client, monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_browse(game, page, page_size, sort="name", set_id=None):
        calls.append({"game": game, "sort": sort, "set_id": set_id})
        return {"cards": [_card("n1", year=2026)], "total": 20359, "source": "mirror"}

    monkeypatch.setattr(catalog_browse_service, "browse_catalog", fake_browse)

    resp = await client.get(
        "/v1/public/search", params={"q": "newest pokemon", "page_size": 24}
    )
    data = resp.json()["data"]
    assert calls == [{"game": "pokemon", "sort": "newest", "set_id": None}]
    assert data["total"] == 20359
    assert {"Newest first", "Pokémon"} <= set(data["interpreted"]["chips"])


@pytest.mark.asyncio
async def test_price_and_sort_shape_the_pooled_results(client, monkeypatch):
    pool = [
        _card("a", "Charizard V", 60.0),
        _card("b", "Charizard VMAX", 30.0),
        _card("c", "Charizard ex", 10.0),
        _card("d", "Charizard promo", None),  # unpriced → dropped by the band
    ]
    seen: dict[str, Any] = {}

    async def fake_search(q: str, tcg: str, limit: int) -> dict[str, Any]:
        seen.update({"q": q, "tcg": tcg})
        return {"results": pool, "total": len(pool), "source": "mixed"}

    monkeypatch.setattr(card_search_service, "search_cards", fake_search)

    resp = await client.get(
        "/v1/public/search",
        params={"q": "cheapest charizard under $50", "page_size": 24},
    )
    data = resp.json()["data"]
    # The modifiers were consumed — upstream saw only the card name.
    assert seen == {"q": "charizard", "tcg": "all"}
    assert [c["id"] for c in data["results"]] == ["c", "b"]  # ≤$50, price asc
    assert data["total"] == 2
    assert {"Cheapest first", "Under $50"} <= set(data["interpreted"]["chips"])


@pytest.mark.asyncio
async def test_explicit_params_beat_parsed_intent(client, monkeypatch):
    pool = [
        _card("cheap", price=5.0, year=2024),
        _card("mid", price=50.0, year=1999),
        _card("dear", price=500.0, year=2010),
    ]

    async def fake_search(q: str, tcg: str, limit: int) -> dict[str, Any]:
        return {"results": pool, "total": len(pool), "source": "mixed"}

    monkeypatch.setattr(card_search_service, "search_cards", fake_search)

    # The query says "newest", the explicit param says price_desc — param wins.
    resp = await client.get(
        "/v1/public/search",
        params={"q": "newest pikachu", "sort": "price_desc", "page_size": 24},
    )
    data = resp.json()["data"]
    assert [c["id"] for c in data["results"]] == ["dear", "mid", "cheap"]
    # The interpretation is still echoed so the client can render the chip.
    assert data["interpreted"]["sort"] == "newest"


@pytest.mark.asyncio
async def test_plain_query_keeps_fast_paged_path(client, monkeypatch):
    async def fake_paged(q, tcg, page, page_size, langs):
        return {
            "results": [_card("p1", "Pikachu")],
            "total": 177,
            "source": "pokemontcg",
        }

    async def boom(*a, **k):  # the pooled path must NOT be hit
        raise AssertionError("pooled search used for a plain query")

    monkeypatch.setattr(card_search_service, "search_cards_paged", fake_paged)
    monkeypatch.setattr(card_search_service, "search_cards", boom)

    resp = await client.get(
        "/v1/public/search", params={"q": "pikachu", "page_size": 24}
    )
    data = resp.json()["data"]
    assert data["total"] == 177
    assert data["interpreted"] is None  # nothing was parsed → nothing to echo


@pytest.mark.asyncio
async def test_game_alias_narrows_the_fast_path(client, monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_paged(q, tcg, page, page_size, langs):
        seen.update({"q": q, "tcg": tcg})
        return {"results": [], "total": 0, "source": "pokemontcg"}

    monkeypatch.setattr(card_search_service, "search_cards_paged", fake_paged)

    resp = await client.get(
        "/v1/public/search", params={"q": "pikachu pokemon", "page_size": 24}
    )
    data = resp.json()["data"]
    # Game parsed out of the text; the fast true-pagination path stays valid.
    assert seen == {"q": "pikachu", "tcg": "pokemon"}
    assert data["interpreted"]["chips"] == ["Pokémon"]


@pytest.mark.asyncio
async def test_rarity_vocab_filters_the_pool(client, monkeypatch):
    pool = [
        _card("s1", "Pikachu", 20.0, rarity="Secret Rare"),
        _card("s2", "Pikachu", 10.0, rarity="Common"),
    ]

    async def fake_search(q: str, tcg: str, limit: int) -> dict[str, Any]:
        return {"results": pool, "total": len(pool), "source": "mixed"}

    monkeypatch.setattr(card_search_service, "search_cards", fake_search)

    resp = await client.get(
        "/v1/public/search",
        params={"q": "secret rare pikachu", "page_size": 24},
    )
    data = resp.json()["data"]
    assert [c["id"] for c in data["results"]] == ["s1"]
    assert "Secret rare" in data["interpreted"]["chips"]
