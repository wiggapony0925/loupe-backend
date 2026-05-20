"""``GET /v1/providers/status`` — shape + content."""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_ok


@pytest.mark.asyncio
async def test_providers_status(client):
    resp = await client.get("/v1/providers/status")
    body = assert_envelope_ok(resp)
    ids = {p["id"] for p in body["providers"]}
    assert ids == {
        "ebay",
        "psa",
        "tcgplayer",
        "pricecharting",
        "130point",
        "gocollect",
        "pokemon_tcg",
        "tcgcsv",
        "justtcg",
    }
    for row in body["providers"]:
        assert isinstance(row["configured"], bool)
        assert isinstance(row["capabilities"], list)
