"""PriceCharting as a language-tagged Pokémon catalog source.

Our pokemontcg.io catalog is English-only; PriceCharting carries Japanese (etc.)
printings, tagged via the set/console name. These assert we parse that language,
surface those cards in search only when asked, advertise the language, and open
a PriceCharting card in detail. All offline (the products/product fetch is
monkeypatched)."""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.pricecharting import catalog as pc
from app.services.catalog import card_search_service as css


def test_language_from_console():
    assert pc.language_from_console("Pokemon Japanese Base Set") == "ja"
    assert pc.language_from_console("Pokemon Base Set") == "en"
    assert pc.language_from_console("Pokemon German Neo Genesis") == "de"


def test_parse_product_pokemon_only():
    parsed = pc.parse_product(
        {
            "id": "6910",
            "product-name": "Charizard #4",
            "console-name": "Pokemon Japanese Base Set",
        }
    )
    assert parsed == {
        "id": "pricecharting:6910",
        "pc_id": "6910",
        "name": "Charizard",
        "number": "4",
        "console": "Pokemon Japanese Base Set",
        "set_name": "Japanese Base Set",
        "language": "ja",
    }
    # A non-Pokémon product (video game) is ignored.
    assert (
        pc.parse_product({"id": "1", "product-name": "Metroid", "console-name": "NES"})
        is None
    )


def test_from_pricecharting_card_shape():
    parsed = pc.parse_product(
        {
            "id": "6910",
            "product-name": "Charizard #4",
            "console-name": "Pokemon Japanese Base Set",
        }
    )
    card = css._from_pricecharting(parsed)
    assert card["id"] == "pricecharting:6910"
    assert card["name"] == "Charizard"
    assert card["tcg"] == "pokemon"
    assert card["source"] == "pricecharting"
    assert card["set_name"] == "Japanese Base Set"
    assert card["attributes"]["language"] == "ja"


@pytest.mark.asyncio
async def test_merge_appends_japanese_only_when_requested(monkeypatch):
    async def fake_products(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "id": "6910",
                "product-name": "Charizard #4",
                "console-name": "Pokemon Japanese Base Set",
            },
            {  # English printing — must NOT be added (dedup vs the EN catalog)
                "id": "7000",
                "product-name": "Charizard #4",
                "console-name": "Pokemon Base Set",
            },
        ]

    monkeypatch.setattr(pc, "search_products", fake_products)
    monkeypatch.setattr(pc, "configured", lambda: True)
    body = {
        "results": [{"id": "pokemontcg:base1-4"}],
        "total": 1,
        "source": "pokemontcg",
    }

    # English-only request → no PriceCharting call, body unchanged.
    same = await css._merge_pricecharting_pokemon(dict(body), "charizard", ["en"], 1)
    assert same["total"] == 1

    # Japanese requested → the JA printing is appended, the EN one is skipped.
    merged = await css._merge_pricecharting_pokemon(
        dict(body), "charizard", ["en", "ja"], 1
    )
    assert merged["total"] == 2
    added = [c for c in merged["results"] if c["id"] == "pricecharting:6910"]
    assert len(added) == 1
    assert added[0]["attributes"]["language"] == "ja"
    assert not [c for c in merged["results"] if c["id"] == "pricecharting:7000"]

    # Page 2 doesn't re-append (the products feed isn't paginated).
    p2 = await css._merge_pricecharting_pokemon(dict(body), "charizard", ["ja"], 2)
    assert p2["total"] == 1


@pytest.mark.asyncio
async def test_available_languages_advertises_ja(monkeypatch):
    async def fake_mirror_langs(tcg):
        return ["en"]

    monkeypatch.setattr(
        css.pokemon_mirror_service, "mirror_languages", fake_mirror_langs
    )
    monkeypatch.setattr(pc, "configured", lambda: True)
    assert await css.available_languages("pokemon") == ["en", "ja"]
    # Not advertised for other games or when unconfigured.
    monkeypatch.setattr(pc, "configured", lambda: False)
    assert await css.available_languages("pokemon") == ["en"]


@pytest.mark.asyncio
async def test_get_card_opens_a_pricecharting_card(monkeypatch):
    async def fake_get_product(pc_id: str) -> dict[str, Any]:
        return {
            "status": "success",
            "id": pc_id,
            "product-name": "Charizard #4",
            "console-name": "Pokemon Japanese Base Set",
            "loose-price": 50000,  # $500 raw
            "manual-only-price": 300000,  # PSA 10 $3000
        }

    monkeypatch.setattr(pc, "get_product", fake_get_product)
    card = await css.get_card("pricecharting:6910")
    assert card is not None
    assert card["name"] == "Charizard"
    assert card["attributes"]["language"] == "ja"
    assert card["pricing_summary"]["market"]["amount"] == 500.0
