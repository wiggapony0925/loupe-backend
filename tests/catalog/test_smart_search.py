"""Smart search across ALL games — typo tolerance + collector-number search.

Verifies the query-parsing + fuzzy-fallback work end-to-end through
``card_search_service.search_cards`` for every provider we offer:
Pokémon / Magic / Yu-Gi-Oh (Postgres mirror) and Digimon / One Piece
(cached catalog via ``_filter_catalog``).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.catalog import card_search_service
from app.services.catalog import pokemon_mirror_service as mirror


@pytest.fixture(autouse=True)
def _tiny_ready_floor(monkeypatch):
    """Fixture catalogs are tiny — drop the "really synced" floor + no cache."""
    monkeypatch.setattr(mirror, "_READY_MIN_CARDS", 1)
    mirror.reset_ready_cache()

    async def _noop_get(_key: str) -> None:
        return None

    async def _noop_set(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(card_search_service, "_cache_get", _noop_get)
    monkeypatch.setattr(card_search_service, "_cache_set", _noop_set)


@pytest.fixture
def live_must_not_be_called(monkeypatch):
    """Mirror is ready, so no live upstream should ever be hit."""

    async def _boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("live upstream called despite a ready mirror")

    monkeypatch.setattr(card_search_service.scryfall, "search_cards", _boom)
    monkeypatch.setattr(card_search_service.ygoprodeck, "search_cards", _boom)
    monkeypatch.setattr(card_search_service.pokemon_tcg, "search_cards", _boom)


async def _seed(
    *,
    tcg: str,
    source: str,
    cid: str,
    upstream_id: str,
    name: str,
    number: str,
    set_id: str = "set1",
    set_name: str = "Test Set",
) -> None:
    """Insert one mirror card in the raw provider payload shape."""
    from app.models.catalog_mirror import CatalogMirrorCard

    bare = number.split("/", 1)[0].lstrip("0") or number
    if tcg == "magic":
        payload = {
            "id": upstream_id,
            "name": name,
            "set": set_id,
            "set_name": set_name,
            "collector_number": number,
            "rarity": "rare",
            "released_at": "1999-01-01",
            "prices": {"usd": "1.00"},
            "image_uris": {"small": "s", "normal": "n"},
        }
    elif tcg == "yugioh":
        payload = {
            "id": int(upstream_id) if upstream_id.isdigit() else upstream_id,
            "name": name,
            "card_images": [{"image_url": "u", "image_url_small": "s"}],
            "card_sets": [
                {"set_name": set_name, "set_code": number, "set_rarity": "Rare"}
            ],
            "card_prices": [{"tcgplayer_price": "1.00"}],
        }
    else:  # pokemon
        payload = {
            "id": upstream_id,
            "name": name,
            "number": number,
            "set": {"id": set_id, "name": set_name, "releaseDate": "1999/01/09"},
            "images": {"small": "s", "large": "l"},
            "rarity": "Rare",
        }

    maker = mirror._sessionmaker()
    async with maker() as s:
        s.add(
            CatalogMirrorCard(
                id=cid,
                source=source,
                tcg=tcg,
                upstream_id=upstream_id,
                set_id=set_id,
                set_name=set_name,
                name=name,
                name_lower=name.lower(),
                number=number,
                bare_number=bare,
                number_int=int(bare) if bare.isdigit() else None,
                rarity="Rare",
                language="en",
                release_date="1999/01/09",
                sort_price=1.0,
                payload=payload,
            )
        )
        await s.commit()


async def _seed_pokemon() -> None:
    await _seed(
        tcg="pokemon",
        source="pokemontcg",
        cid="pokemontcg:base1-4",
        upstream_id="base1-4",
        name="Charizard",
        number="4/102",
    )
    await _seed(
        tcg="pokemon",
        source="pokemontcg",
        cid="pokemontcg:base1-58",
        upstream_id="base1-58",
        name="Pikachu",
        number="58/102",
    )


async def _seed_magic() -> None:
    await _seed(
        tcg="magic",
        source="scryfall",
        cid="scryfall:m-lotus",
        upstream_id="m-lotus",
        name="Black Lotus",
        number="232",
    )
    await _seed(
        tcg="magic",
        source="scryfall",
        cid="scryfall:m-bolt",
        upstream_id="m-bolt",
        name="Lightning Bolt",
        number="161",
    )


async def _seed_yugioh() -> None:
    await _seed(
        tcg="yugioh",
        source="ygoprodeck",
        cid="ygoprodeck:89631139",
        upstream_id="89631139",
        name="Blue-Eyes White Dragon",
        number="LOB-001",
    )
    await _seed(
        tcg="yugioh",
        source="ygoprodeck",
        cid="ygoprodeck:46986414",
        upstream_id="46986414",
        name="Dark Magician",
        number="LOB-005",
    )


# ─────────────────────────── typo tolerance ───────────────────────────


@pytest.mark.asyncio
async def test_pokemon_typo(db_engine, live_must_not_be_called):
    await _seed_pokemon()
    mirror.reset_ready_cache("pokemon")
    body = await card_search_service.search_cards("charzard", "pokemon", 10)
    names = [r["name"] for r in body["results"]]
    assert "Charizard" in names


@pytest.mark.asyncio
async def test_magic_typo(db_engine, live_must_not_be_called):
    await _seed_magic()
    mirror.reset_ready_cache("magic")
    body = await card_search_service.search_cards("blak lotus", "magic", 10)
    names = [r["name"] for r in body["results"]]
    assert "Black Lotus" in names


@pytest.mark.asyncio
async def test_yugioh_typo(db_engine, live_must_not_be_called):
    await _seed_yugioh()
    mirror.reset_ready_cache("yugioh")
    body = await card_search_service.search_cards("blue eyes", "yugioh", 10)
    names = [r["name"] for r in body["results"]]
    assert "Blue-Eyes White Dragon" in names


# ─────────────────────────── card numbers ─────────────────────────────


@pytest.mark.asyncio
async def test_pokemon_number_fraction(db_engine, live_must_not_be_called):
    await _seed_pokemon()
    mirror.reset_ready_cache("pokemon")
    body = await card_search_service.search_cards("charizard 4/102", "pokemon", 10)
    top = body["results"][0]
    assert top["name"] == "Charizard"


@pytest.mark.asyncio
async def test_pokemon_number_only(db_engine, live_must_not_be_called):
    await _seed_pokemon()
    mirror.reset_ready_cache("pokemon")
    body = await card_search_service.search_cards("58/102", "pokemon", 10)
    names = [r["name"] for r in body["results"]]
    assert "Pikachu" in names


@pytest.mark.asyncio
async def test_magic_number(db_engine, live_must_not_be_called):
    await _seed_magic()
    mirror.reset_ready_cache("magic")
    body = await card_search_service.search_cards("lightning bolt 161", "magic", 10)
    top = body["results"][0]
    assert top["name"] == "Lightning Bolt"


@pytest.mark.asyncio
async def test_yugioh_number_in_setcode(db_engine, live_must_not_be_called):
    await _seed_yugioh()
    mirror.reset_ready_cache("yugioh")
    body = await card_search_service.search_cards("dark magician 5", "yugioh", 10)
    names = [r["name"] for r in body["results"]]
    assert "Dark Magician" in names


# ─────────────────────── Digimon / One Piece ──────────────────────────

DIGIMON_CATALOG = [
    {
        "id": "digimon:BT1-001",
        "name": "Agumon",
        "number": "BT1-001",
        "set_name": "Booster 1",
        "tcg": "digimon",
        "source": "digimoncard",
    },
    {
        "id": "digimon:BT1-010",
        "name": "Greymon",
        "number": "BT1-010",
        "set_name": "Booster 1",
        "tcg": "digimon",
        "source": "digimoncard",
    },
]

ONEPIECE_CATALOG = [
    {
        "id": "onepiece:OP01-001",
        "name": "Monkey D. Luffy",
        "number": "OP01-001",
        "set_name": "Romance Dawn",
        "tcg": "onepiece",
        "source": "apitcg-onepiece",
    },
    {
        "id": "onepiece:OP01-025",
        "name": "Roronoa Zoro",
        "number": "OP01-025",
        "set_name": "Romance Dawn",
        "tcg": "onepiece",
        "source": "apitcg-onepiece",
    },
]


@pytest.mark.asyncio
async def test_digimon_typo(db_engine, monkeypatch):
    async def _catalog() -> list[dict[str, Any]]:
        return DIGIMON_CATALOG

    monkeypatch.setattr(card_search_service, "digimon_catalog", _catalog)
    body = await card_search_service.search_cards("agumn", "digimon", 10)
    names = [r["name"] for r in body["results"]]
    assert "Agumon" in names


@pytest.mark.asyncio
async def test_digimon_number(db_engine, monkeypatch):
    async def _catalog() -> list[dict[str, Any]]:
        return DIGIMON_CATALOG

    monkeypatch.setattr(card_search_service, "digimon_catalog", _catalog)
    body = await card_search_service.search_cards("greymon BT1-010", "digimon", 10)
    names = [r["name"] for r in body["results"]]
    assert "Greymon" in names


@pytest.mark.asyncio
async def test_onepiece_typo(db_engine, monkeypatch):
    async def _catalog() -> list[dict[str, Any]]:
        return ONEPIECE_CATALOG

    monkeypatch.setattr(card_search_service, "onepiece_catalog", _catalog)
    body = await card_search_service.search_cards("luffy", "onepiece", 10)
    names = [r["name"] for r in body["results"]]
    assert "Monkey D. Luffy" in names


@pytest.mark.asyncio
async def test_onepiece_number(db_engine, monkeypatch):
    async def _catalog() -> list[dict[str, Any]]:
        return ONEPIECE_CATALOG

    monkeypatch.setattr(card_search_service, "onepiece_catalog", _catalog)
    body = await card_search_service.search_cards("zoro OP01-025", "onepiece", 10)
    names = [r["name"] for r in body["results"]]
    assert "Roronoa Zoro" in names
