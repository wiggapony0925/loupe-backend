"""Server-rendered card share/OG page (/v1/share/card/{id})."""

from __future__ import annotations

from typing import Any

import pytest

from app.routers.public import share


@pytest.mark.asyncio
async def test_share_renders_card_og_tags(client, monkeypatch):
    async def fake_get_card(card_id: str) -> dict[str, Any]:
        return {
            "id": card_id,
            "name": "Charizard",
            "tcg": "pokemon",
            "set_name": "Base",
            "rarity": "Rare Holo",
            "images": {"large": {"url": "https://img/charizard-large.png"}},
            "image_url": "https://img/charizard-small.png",
            "pricing_summary": {"market": {"amount": 630.39, "currency": "USD"}},
        }

    monkeypatch.setattr(share.card_search_service, "get_card", fake_get_card)

    resp = await client.get("/v1/share/card/pokemontcg:base1-4")
    assert resp.status_code == 200
    body = resp.text
    assert 'property="og:image" content="https://img/charizard-large.png"' in body
    assert "Charizard" in body and "Base" in body
    assert "$630.39" in body
    assert 'name="twitter:card" content="summary_large_image"' in body
    # Humans get redirected to the SPA.
    assert "/cards/pokemontcg:base1-4" in body


@pytest.mark.asyncio
async def test_share_falls_back_to_banner_when_card_missing(client, monkeypatch):
    async def none_get_card(card_id: str) -> None:
        return None

    monkeypatch.setattr(share.card_search_service, "get_card", none_get_card)

    resp = await client.get("/v1/share/card/unknown:404")
    assert resp.status_code == 200
    # Falls back to the generic banner, never errors.
    assert "og-image.png" in resp.text
    assert 'property="og:title"' in resp.text


@pytest.mark.asyncio
async def test_share_never_5xx_on_lookup_error(client, monkeypatch):
    async def boom(card_id: str) -> dict[str, Any]:
        raise RuntimeError("upstream down")

    monkeypatch.setattr(share.card_search_service, "get_card", boom)

    resp = await client.get("/v1/share/card/anything")
    assert resp.status_code == 200  # degrades to banner, never 500
