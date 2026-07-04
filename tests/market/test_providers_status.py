"""``GET /v1/providers/status`` — shape + content."""

from __future__ import annotations

import pytest

from app.integrations.registry import reset_registry
from tests.conftest import assert_envelope_ok


@pytest.fixture(autouse=True)
def _real_registry():
    """This test asserts the REAL provider catalog — clear the empty-registry
    override installed by conftest. ``status()`` is config-only, no network."""
    reset_registry()
    yield
    reset_registry()


@pytest.mark.asyncio
async def test_providers_status(client):
    resp = await client.get("/v1/providers/status")
    body = assert_envelope_ok(resp)
    ids = {p["id"] for p in body["providers"]}
    assert ids == {
        "ebay",
        "stockx",
        "psa",
        "tcgplayer",
        "pricecharting",
        "130point",
        "gocollect",
        "pokemon_tcg",
        "tcgcsv",
        "tcgdex",
        "justtcg",
        "pokemonpricetracker",
    }
    for row in body["providers"]:
        assert isinstance(row["configured"], bool)
        assert isinstance(row["capabilities"], list)
