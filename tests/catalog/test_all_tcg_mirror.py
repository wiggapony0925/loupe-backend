"""Magic (Scryfall) + Yu-Gi-Oh (YGOPRODeck) catalog mirror — sync + search.

Everything runs offline: the bulk/dump fetch is monkeypatched with fixtures and
the live upstream clients are patched to *fail loudly* wherever a mirror hit
must make them unnecessary. Mirrors the Pokémon mirror test posture.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.catalog import card_search_service
from app.services.catalog import pokemon_mirror_service as mirror

# ------------------------------------------------------------------ fixtures

SCRYFALL_CARDS = [
    {
        "id": "m-lotus",
        "name": "Black Lotus",
        "set": "lea",
        "set_name": "Limited Edition Alpha",
        "collector_number": "232",
        "rarity": "rare",
        "released_at": "1993-08-05",
        "prices": {"usd": "9999.99"},
        "lang": "en",
        "image_uris": {"small": "s", "normal": "n"},
    },
    {
        "id": "m-bolt",
        "name": "Lightning Bolt",
        "set": "lea",
        "set_name": "Limited Edition Alpha",
        "collector_number": "161",
        "rarity": "common",
        "released_at": "1993-08-05",
        "prices": {"usd": "4.50"},
        "image_uris": {"small": "s", "normal": "n"},
    },
]

BULK_INDEX = {
    "data": [
        {"type": "oracle_cards", "download_uri": "https://scry/oracle.json"},
        {"type": "default_cards", "download_uri": "https://scry/default.json"},
    ]
}

YGO_DUMP = {
    "data": [
        {
            "id": 89631139,
            "name": "Blue-Eyes White Dragon",
            "type": "Normal Monster",
            "card_sets": [
                {
                    "set_name": "Legend of Blue Eyes White Dragon",
                    "set_code": "LOB-001",
                    "set_rarity": "Ultra Rare",
                }
            ],
            "card_images": [{"image_url": "u", "image_url_small": "s"}],
            "card_prices": [{"tcgplayer_price": "5.00"}],
        },
        {
            "id": 46986414,
            "name": "Dark Magician",
            "type": "Normal Monster",
            "card_sets": [
                {"set_name": "LOB", "set_code": "LOB-005", "set_rarity": "Ultra Rare"}
            ],
            "card_images": [{"image_url": "u", "image_url_small": "s"}],
            "card_prices": [{"tcgplayer_price": "3.00"}],
        },
    ]
}


@pytest.fixture(autouse=True)
def _tiny_ready_floor(monkeypatch):
    """The fixture catalogs are tiny; drop the "is it really synced" floor."""
    monkeypatch.setattr(mirror, "_READY_MIN_CARDS", 1)
    mirror.reset_ready_cache()


@pytest.fixture
def patched_json(monkeypatch):
    async def _fake(url: str, *, timeout_s: float = 60.0) -> Any:
        if url == mirror._MAGIC_BULK_URL:
            return BULK_INDEX
        if url == "https://scry/default.json":
            return SCRYFALL_CARDS
        if url == mirror._YGO_DUMP_URL:
            return YGO_DUMP
        raise AssertionError(f"unexpected fetch url: {url}")

    monkeypatch.setattr(mirror, "_fetch_json", _fake)


@pytest.fixture
def live_must_not_be_called(monkeypatch):
    async def _boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("live upstream was called despite a ready mirror")

    monkeypatch.setattr(card_search_service.scryfall, "search_cards", _boom)
    monkeypatch.setattr(card_search_service.ygoprodeck, "search_cards", _boom)


# --------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_magic_sync_then_search_from_mirror(
    db_engine, patched_json, live_must_not_be_called
):
    stats = await mirror.sync_magic_from_bulk()
    mirror.reset_ready_cache("magic")
    assert stats == {"tcg": "magic", "cards_total": 2, "cards_synced": 2}
    assert await mirror.mirror_ready("magic") is True

    body = await card_search_service.search_cards("black lotus", "magic", 10)
    assert body["source"] == "scryfall"
    names = [r["name"] for r in body["results"]]
    assert "Black Lotus" in names
    # Rendered via _from_scryfall from the raw mirror payload.
    lotus = next(r for r in body["results"] if r["name"] == "Black Lotus")
    assert lotus["tcg"] == "magic"
    assert lotus["set_name"] == "Limited Edition Alpha"
    assert lotus["pricing_summary"]["market"]["amount"] == 9999.99

    # True local pagination.
    paged = await card_search_service.search_cards_paged(
        "lightning", "magic", page=1, page_size=1
    )
    assert paged["total"] == 1
    assert paged["results"][0]["name"] == "Lightning Bolt"


@pytest.mark.asyncio
async def test_yugioh_sync_then_search_from_mirror(
    db_engine, patched_json, live_must_not_be_called
):
    stats = await mirror.sync_yugioh_from_dump()
    mirror.reset_ready_cache("yugioh")
    assert stats == {"tcg": "yugioh", "cards_total": 2, "cards_synced": 2}
    assert await mirror.mirror_ready("yugioh") is True

    body = await card_search_service.search_cards("blue-eyes", "yugioh", 10)
    assert body["source"] == "ygoprodeck"
    assert any(r["name"] == "Blue-Eyes White Dragon" for r in body["results"])

    paged = await card_search_service.search_cards_paged(
        "dark magician", "yugioh", page=1, page_size=5
    )
    assert paged["total"] == 1
    assert paged["results"][0]["name"] == "Dark Magician"


@pytest.mark.asyncio
async def test_sync_is_idempotent(db_engine, patched_json):
    await mirror.sync_yugioh_from_dump()
    stats2 = await mirror.sync_yugioh_from_dump()
    assert stats2["cards_synced"] == 2  # upsert, no duplicates
    mirror.reset_ready_cache("yugioh")
    body = await card_search_service.search_cards_paged(
        "blue-eyes", "yugioh", page=1, page_size=50
    )
    assert body["total"] == 1  # still one Blue-Eyes row, not two


@pytest.mark.asyncio
async def test_empty_magic_mirror_falls_back_to_live(db_engine, monkeypatch):
    """No sync ⇒ mirror not ready ⇒ the live Scryfall path still serves."""

    async def _fake_scry(q: str, page: int = 1, **k: Any) -> dict[str, Any]:
        return {
            "data": [
                {
                    "id": "live-1",
                    "name": "Counterspell",
                    "set": "3ed",
                    "set_name": "Revised Edition",
                    "collector_number": "54",
                    "prices": {"usd": "2.00"},
                    "image_uris": {"small": "s", "normal": "n"},
                }
            ]
        }

    monkeypatch.setattr(card_search_service.scryfall, "search_cards", _fake_scry)
    body = await card_search_service.search_cards("counterspell", "magic", 10)
    assert any(r["name"] == "Counterspell" for r in body["results"])
