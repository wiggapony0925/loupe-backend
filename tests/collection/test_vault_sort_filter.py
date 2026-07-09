"""Backend-driven vault sort + filter — the competitor-parity additions to
``/v1/grades``: name sort, graded/raw filters, watchlist filter."""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_ok
from tests.factories import make_card


async def _hold(client, auth_headers, db_session, *, name: str, house: str) -> dict:
    card = await make_card(db_session, name=name)
    body = {"card_id": str(card.id), "grade": "9.0", "house": house}
    r = await client.post("/v1/grades", headers=auth_headers, json=body)
    assert r.status_code == 201, r.text
    return {"card_id": str(card.id), **r.json()["data"]}


@pytest.mark.asyncio
async def test_name_sort_ascending_and_descending(client, auth_headers, db_session):
    await _hold(client, auth_headers, db_session, name="Zapdos", house="loupe")
    await _hold(client, auth_headers, db_session, name="Abra", house="loupe")

    asc = assert_envelope_ok(
        await client.get("/v1/grades?sort=name_asc", headers=auth_headers)
    )
    assert [r["card_name"] for r in asc] == ["Abra", "Zapdos"]

    desc = assert_envelope_ok(
        await client.get("/v1/grades?sort=name_desc", headers=auth_headers)
    )
    assert [r["card_name"] for r in desc] == ["Zapdos", "Abra"]


@pytest.mark.asyncio
async def test_graded_and_raw_filters(client, auth_headers, db_session):
    await _hold(client, auth_headers, db_session, name="Slabbed", house="psa")
    await _hold(client, auth_headers, db_session, name="Loose", house="loupe")

    graded = assert_envelope_ok(
        await client.get("/v1/grades?graded_only=true", headers=auth_headers)
    )
    assert [r["card_name"] for r in graded] == ["Slabbed"]

    raw = assert_envelope_ok(
        await client.get("/v1/grades?raw_only=true", headers=auth_headers)
    )
    assert [r["card_name"] for r in raw] == ["Loose"]


@pytest.mark.asyncio
async def test_watchlist_filter(client, auth_headers, db_session):
    watched = await _hold(
        client, auth_headers, db_session, name="Watched", house="loupe"
    )
    await _hold(client, auth_headers, db_session, name="Ignored", house="loupe")

    # Star the first card.
    r = await client.post(
        "/v1/watchlist", headers=auth_headers, json={"card_id": watched["card_id"]}
    )
    assert r.status_code in (200, 201), r.text

    rows = assert_envelope_ok(
        await client.get("/v1/grades?watchlist=true", headers=auth_headers)
    )
    assert [r["card_name"] for r in rows] == ["Watched"]


@pytest.mark.asyncio
async def test_bad_sort_rejected(client, auth_headers, db_session):
    r = await client.get("/v1/grades?sort=nonsense", headers=auth_headers)
    assert r.status_code == 400
