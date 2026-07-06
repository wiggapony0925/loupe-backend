"""Pokémon catalog mirror — sync, reads, and wiring into the public surfaces.

Everything runs offline: the dump fetch is monkeypatched with fixture data and
the live pokemontcg client is patched to *fail loudly* wherever a mirror hit
must make it unnecessary.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.catalog import (
    card_search_service,
    catalog_browse_service,
)
from app.services.catalog import (
    pokemon_mirror_service as mirror,
)

# ------------------------------------------------------------------ fixtures

SET_A = {
    "id": "tsta",
    "name": "Test Alpha",
    "series": "Testing",
    "printedTotal": 3,
    "total": 3,
    "releaseDate": "2026/05/22",
    "images": {
        "symbol": "https://img/tsta/sym.png",
        "logo": "https://img/tsta/logo.png",
    },
}
SET_B = {
    "id": "tstb",
    "name": "Test Beta",
    "series": "Testing",
    "printedTotal": 2,
    "total": 2,
    "releaseDate": "1999/01/09",
    "images": {
        "symbol": "https://img/tstb/sym.png",
        "logo": "https://img/tstb/logo.png",
    },
}

CARDS_A = [
    {
        "id": "tsta-1",
        "name": "Pikachu",
        "number": "1",
        "rarity": "Common",
        "supertype": "Pokémon",
        "images": {"small": "https://img/tsta-1/s", "large": "https://img/tsta-1/l"},
    },
    {
        "id": "tsta-2",
        "name": "Charizard ex",
        "number": "2",
        "rarity": "Double Rare",
        "supertype": "Pokémon",
        "images": {"small": "https://img/tsta-2/s", "large": "https://img/tsta-2/l"},
    },
    {
        "id": "tsta-3",
        "name": "Ultra Ball",
        "number": "3",
        "rarity": "Uncommon",
        "supertype": "Trainer",
        "images": {"small": "https://img/tsta-3/s", "large": "https://img/tsta-3/l"},
    },
]
CARDS_B = [
    {
        "id": "tstb-1",
        "name": "Pikachu",
        "number": "058/102",
        "rarity": "Common",
        "supertype": "Pokémon",
        "images": {"small": "https://img/tstb-1/s", "large": "https://img/tstb-1/l"},
    },
    {
        "id": "tstb-2",
        "name": "Blastoise",
        "number": "2",
        "rarity": "Rare Holo",
        "supertype": "Pokémon",
        "images": {"small": "https://img/tstb-2/s", "large": "https://img/tstb-2/l"},
    },
]

DUMP = {
    "sets/en.json": [SET_A, SET_B],
    "cards/en/tsta.json": CARDS_A,
    "cards/en/tstb.json": CARDS_B,
}


@pytest.fixture
def patched_dump(monkeypatch):
    async def _fake_fetch(path: str) -> Any:
        return DUMP[path]

    monkeypatch.setattr(mirror, "_fetch_dump", _fake_fetch)
    return DUMP


@pytest.fixture
def live_api_must_not_be_called(monkeypatch):
    """Fail the test if any code path falls through to the live API."""

    async def _boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("live pokemontcg.io API was called")

    monkeypatch.setattr(card_search_service.pokemon_tcg, "search_cards", _boom)
    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", _boom)
    monkeypatch.setattr(catalog_browse_service.pokemon_tcg, "search_cards", _boom)


async def _synced(patched_dump) -> dict[str, Any]:
    stats = await mirror.sync_pokemon_from_dump()
    mirror.reset_ready_cache()
    # Readiness normally needs a real catalog's worth of rows; the fixture
    # catalog is 5 cards, so drop the floor for the tests.
    mirror._ready_cache = None
    return stats


@pytest.fixture(autouse=True)
def _tiny_ready_floor(monkeypatch):
    monkeypatch.setattr(mirror, "_READY_MIN_CARDS", 1)


# --------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_sync_populates_mirror(db_engine, patched_dump):
    stats = await _synced(patched_dump)
    assert stats["sets_synced"] == 2
    assert stats["cards_synced"] == 5
    assert stats["errors"] == 0

    status = await mirror.mirror_status()
    assert status["ready"] is True
    assert status["cards"] == 5
    assert status["sets"] == 2

    # Payload got the set object injected (API shape).
    payload = await mirror.get_pokemon_by_id("tsta-1")
    assert payload is not None
    assert payload["set"]["name"] == "Test Alpha"
    assert payload["set"]["releaseDate"] == "2026/05/22"


@pytest.mark.asyncio
async def test_sync_is_idempotent_and_skips_complete_sets(db_engine, patched_dump):
    await _synced(patched_dump)
    stats2 = await mirror.sync_pokemon_from_dump()
    assert stats2["sets_synced"] == 0
    assert stats2["sets_skipped"] == 2
    assert (await mirror.mirror_status())["cards"] == 5


@pytest.mark.asyncio
async def test_resync_preserves_hydrated_prices(db_engine, patched_dump, monkeypatch):
    await _synced(patched_dump)

    # Hydrate prices for set A from a fake live API.
    async def _fake_live(query: str, page: int = 1, page_size: int = 25, **k: Any):
        assert query == "set.id:tsta"
        return {
            "data": [
                {
                    "id": "tsta-2",
                    "tcgplayer": {
                        "url": "https://t",
                        "updatedAt": "2026/07/06",
                        "prices": {"holofoil": {"market": 123.45, "low": 100.0}},
                    },
                }
            ],
            "totalCount": 1,
        }

    monkeypatch.setattr(mirror.pokemon_tcg, "search_cards", _fake_live)
    updated = await mirror.refresh_set_prices("tsta")
    assert updated == 1

    payload = await mirror.get_pokemon_by_id("tsta-2")
    assert payload["tcgplayer"]["prices"]["holofoil"]["market"] == 123.45

    # A forced identity re-sync (dump has no price blocks) must keep them.
    await mirror.sync_pokemon_from_dump(force=True)
    payload = await mirror.get_pokemon_by_id("tsta-2")
    assert payload["tcgplayer"]["prices"]["holofoil"]["market"] == 123.45
    # And the price sort column survived too.
    browse = await mirror.browse_pokemon(1, 10, "price_desc")
    assert browse["payloads"][0]["id"] == "tsta-2"


@pytest.mark.asyncio
async def test_degraded_price_refresh_never_wipes_prices(
    db_engine, patched_dump, monkeypatch
):
    """A degraded upstream answers cards with EMPTY price dicts — merging
    those must never replace good hydrated prices (wiped real sets in prod)."""
    await _synced(patched_dump)

    good = {
        "data": [
            {
                "id": "tsta-2",
                "tcgplayer": {
                    "url": "https://t",
                    "updatedAt": "2026/07/06",
                    "prices": {"holofoil": {"market": 99.0}},
                },
            }
        ],
        "totalCount": 1,
    }
    degraded = {
        "data": [
            {
                "id": "tsta-2",
                "tcgplayer": {"url": "https://t", "prices": {}},
                "cardmarket": {"prices": {"averageSellPrice": None}},
            }
        ],
        "totalCount": 1,
    }

    async def _live_good(*a: Any, **k: Any):
        return good

    async def _live_degraded(*a: Any, **k: Any):
        return degraded

    monkeypatch.setattr(mirror.pokemon_tcg, "search_cards", _live_good)
    assert await mirror.refresh_set_prices("tsta") == 1

    monkeypatch.setattr(mirror.pokemon_tcg, "search_cards", _live_degraded)
    assert await mirror.refresh_set_prices("tsta") == 0

    payload = await mirror.get_pokemon_by_id("tsta-2")
    assert payload["tcgplayer"]["prices"]["holofoil"]["market"] == 99.0
    browse = await mirror.browse_pokemon(1, 10, "price_desc")
    assert browse["payloads"][0]["id"] == "tsta-2"  # sort_price survived

    # A degraded fetch on a never-hydrated set must leave it STALE so the
    # next walk retries it (not parked "fresh" and priceless for 24h).
    assert await mirror.refresh_set_prices("tstb") == 0
    assert "tstb" in await mirror.stale_price_set_ids(limit=50)


@pytest.mark.asyncio
async def test_browse_sorting_and_set_scope(db_engine, patched_dump):
    await _synced(patched_dump)

    by_name = await mirror.browse_pokemon(1, 10, "name")
    assert [p["name"] for p in by_name["payloads"]] == [
        "Blastoise",
        "Charizard ex",
        "Pikachu",
        "Pikachu",
        "Ultra Ball",
    ]
    assert by_name["total"] == 5

    newest = await mirror.browse_pokemon(1, 10, "newest")
    # 2026 set first, in collector-number order.
    assert [p["id"] for p in newest["payloads"]][:3] == ["tsta-1", "tsta-2", "tsta-3"]

    scoped = await mirror.browse_pokemon(1, 10, "name", set_id="tstb")
    assert scoped["total"] == 2
    assert {p["id"] for p in scoped["payloads"]} == {"tstb-1", "tstb-2"}

    # Pagination math: page 2 of size 2 over 5 rows.
    page2 = await mirror.browse_pokemon(2, 2, "name")
    assert page2["total"] == 5
    assert len(page2["payloads"]) == 2


@pytest.mark.asyncio
async def test_search_and_relaxed_fallback(db_engine, patched_dump):
    await _synced(patched_dump)

    hit = await mirror.search_pokemon("pikachu")
    assert hit["total"] == 2

    # AND of both tokens matches nothing; OR fallback still finds both names.
    relaxed = await mirror.search_pokemon("pikachu blastoise")
    assert relaxed["total"] == 3

    none = await mirror.search_pokemon("zzzz-not-a-card")
    assert none["total"] == 0


@pytest.mark.asyncio
async def test_precise_lookup_pins_collector_number(db_engine, patched_dump):
    await _synced(patched_dump)

    # "058/102" reduces to bare 58 — but our fixture has number 058/102 whose
    # bare form is "58"; a plain Pikachu search would surface both printings.
    rows = await mirror.precise_pokemon("Pikachu", "58")
    assert [r["id"] for r in rows] == ["tstb-1"]

    # OCR-garbled name falls back to number-only.
    rows = await mirror.precise_pokemon("P1kachu5", "58")
    assert [r["id"] for r in rows] == ["tstb-1"]


@pytest.mark.asyncio
async def test_get_card_serves_identity_from_mirror(
    db_engine, patched_dump, live_api_must_not_be_called
):
    await _synced(patched_dump)

    card = await card_search_service.get_card("pokemontcg:tsta-2")
    assert card is not None
    assert card["name"] == "Charizard ex"
    assert card["set_name"] == "Test Alpha"
    assert card["source"] == "pokemontcg"
    assert card["images"]["large"]["url"] == "https://img/tsta-2/l"


@pytest.mark.asyncio
async def test_browse_catalog_service_uses_mirror(
    db_engine, patched_dump, live_api_must_not_be_called
):
    await _synced(patched_dump)

    body = await catalog_browse_service.browse_catalog("pokemon", 1, 24, sort="name")
    assert body["total"] == 5
    assert body["source"] == "pokemontcg"
    assert len(body["cards"]) == 5
    # Set scoping through the public contract (`<source>:<set>` form).
    scoped = await catalog_browse_service.browse_catalog(
        "pokemon", 1, 24, sort="name", set_id="pokemontcg:tsta"
    )
    assert scoped["total"] == 3


@pytest.mark.asyncio
async def test_search_cards_paged_uses_mirror(
    db_engine, patched_dump, live_api_must_not_be_called
):
    await _synced(patched_dump)

    body = await card_search_service.search_cards_paged(
        "pikachu", "pokemon", page=1, page_size=1
    )
    assert body["total"] == 2
    assert len(body["results"]) == 1
    body2 = await card_search_service.search_cards_paged(
        "pikachu", "pokemon", page=2, page_size=1
    )
    assert len(body2["results"]) == 1
    assert body["results"][0]["id"] != body2["results"][0]["id"]


@pytest.mark.asyncio
async def test_public_browse_endpoint_serves_mirror(
    client, patched_dump, live_api_must_not_be_called
):
    await _synced(patched_dump)

    resp = await client.get(
        "/v1/public/browse", params={"game": "pokemon", "page_size": 24}
    )
    assert resp.status_code == 200
    body = resp.json()["data"]  # envelope middleware wraps in {data, meta}
    assert body["total"] == 5
    names = {c["name"] for c in body["cards"]}
    assert "Charizard ex" in names


@pytest.mark.asyncio
async def test_mirror_not_ready_falls_back_to_live(db_engine, monkeypatch):
    """Empty mirror → the live proxy path still serves browse."""

    async def _fake_live(query: str, **kwargs: Any):
        return {
            "data": [
                {
                    "id": "live-1",
                    "name": "Live Card",
                    "number": "1",
                    "set": SET_A,
                    "images": {"small": "s", "large": "l"},
                }
            ],
            "totalCount": 1,
        }

    monkeypatch.setattr(catalog_browse_service.pokemon_tcg, "search_cards", _fake_live)
    body = await catalog_browse_service.browse_catalog("pokemon", 1, 24, sort="name")
    assert body["total"] == 1
    assert body["cards"][0]["name"] == "Live Card"
