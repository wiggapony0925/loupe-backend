"""Public storefront API (/v1/public/*) — server-side filter/sort/paginate."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.catalog import card_search_service
from app.services.market import trending_service


def _card(id_: str, name: str, rarity: str, set_name: str, price: float) -> dict[str, Any]:
    return {
        "id": id_,
        "name": name,
        "rarity": rarity,
        "set_name": set_name,
        "image_url": f"https://img/{id_}.png",
        "pricing_summary": {"market": {"amount": price, "currency": "USD"}},
    }


@pytest.mark.asyncio
async def test_public_search_filters_sorts_paginates_and_facets(client, monkeypatch):
    cards = [
        _card("a", "Bravo", "Rare", "Base", 10.0),
        _card("b", "Alpha", "Common", "Base", 2.0),
        _card("c", "Charlie", "Rare", "Jungle", 5.0),
    ]

    async def fake_search(q: str, tcg: str, limit: int) -> dict[str, Any]:
        return {"results": cards, "total": len(cards), "source": "mixed"}

    monkeypatch.setattr(card_search_service, "search_cards", fake_search)

    resp = await client.get(
        "/v1/public/search",
        params={"q": "x", "rarity": "Rare", "sort": "price_desc", "page": 1, "page_size": 1},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["total"] == 2  # two Rares match the filter
    assert data["page_size"] == 1
    assert len(data["results"]) == 1
    assert data["results"][0]["name"] == "Bravo"  # 10.0 sorts above 5.0
    # facets come from the full pre-filter set
    assert set(data["facets"]["rarities"]) == {"Common", "Rare"}
    assert set(data["facets"]["sets"]) == {"Base", "Jungle"}


@pytest.mark.asyncio
async def test_public_search_page_two(client, monkeypatch):
    cards = [_card(str(i), f"Card {i:02d}", "Rare", "Base", float(i)) for i in range(5)]

    async def fake_search(q: str, tcg: str, limit: int) -> dict[str, Any]:
        return {"results": cards, "total": len(cards), "source": "mixed"}

    monkeypatch.setattr(card_search_service, "search_cards", fake_search)

    resp = await client.get(
        "/v1/public/search", params={"q": "x", "sort": "name", "page": 2, "page_size": 2}
    )
    data = resp.json()["data"]
    assert data["total"] == 5
    assert [c["name"] for c in data["results"]] == ["Card 02", "Card 03"]


@pytest.mark.asyncio
async def test_public_browse_pokemon_paginates(client, monkeypatch):
    from app.services.catalog import catalog_browse_service as svc

    async def fake_search(query: str, page: int = 1, page_size: int = 25):
        return {"data": [{"id": "p1"}, {"id": "p2"}], "totalCount": 18342}

    monkeypatch.setattr(svc.pokemon_tcg, "search_cards", fake_search)
    monkeypatch.setattr(svc, "_from_pokemon", lambda c: {"id": c["id"], "name": c["id"]})

    resp = await client.get("/v1/public/browse", params={"game": "pokemon", "page": 3, "page_size": 24})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 18342  # real catalog size drives the pager
    assert data["page"] == 3
    assert [c["id"] for c in data["cards"]] == ["p1", "p2"]


@pytest.mark.asyncio
async def test_public_browse_unsupported_game_is_empty(client):
    resp = await client.get("/v1/public/browse", params={"game": "lorcana"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["cards"] == []


@pytest.mark.asyncio
async def test_public_trending_value_sort_and_price_ceiling(client, monkeypatch):
    cards = [
        _card("a", "A", "Rare", "S", 3.0),
        _card("b", "B", "Rare", "S", 50.0),
        _card("c", "C", "Rare", "S", 1.0),
    ]

    async def fake_trending(tcg: str = "all", limit: int = 24) -> dict[str, Any]:
        return {"cards": cards, "source": "live"}

    monkeypatch.setattr(trending_service, "get_trending", fake_trending)

    by_value = await client.get("/v1/public/trending", params={"sort": "value", "limit": 2})
    assert [c["id"] for c in by_value.json()["data"]["cards"]] == ["b", "a"]

    cheap = await client.get("/v1/public/trending", params={"max_price": 5})
    assert {c["id"] for c in cheap.json()["data"]["cards"]} == {"a", "c"}
