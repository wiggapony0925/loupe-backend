"""`/v1/grades/count` and `/v1/grades/filters` — the vault filter bar's two
supporting reads.

`count` exists so the UI can show "247 results" while the user is still
dragging sliders, without paying for a page of card payloads; `filters` is the
server-owned list of option labels so web and mobile never drift apart.
"""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_card


async def _hold(client, headers, db_session, *, name: str, house: str, grade: str):
    card = await make_card(db_session, name=name)
    resp = await client.post(
        "/v1/grades",
        headers=headers,
        json={"card_id": str(card.id), "grade": grade, "house": house},
    )
    assert resp.status_code == 201, resp.text
    return card


# ------------------------------------------------------------------- count


@pytest.mark.asyncio
async def test_count_reports_the_number_of_vault_rows(client, auth_headers, db_session):
    await _hold(client, auth_headers, db_session, name="A", house="psa", grade="10.0")
    await _hold(client, auth_headers, db_session, name="B", house="loupe", grade="8.0")

    body = assert_envelope_ok(
        await client.get("/v1/grades/count", headers=auth_headers)
    )
    assert body == {"count": 2}


@pytest.mark.asyncio
async def test_count_is_empty_for_a_collector_with_nothing(client, auth_headers):
    """A fresh account must read as a real zero, not a missing key — the UI
    renders the empty state off this number."""
    body = assert_envelope_ok(
        await client.get("/v1/grades/count", headers=auth_headers)
    )
    assert body == {"count": 0}


@pytest.mark.asyncio
async def test_count_applies_the_same_filters_as_the_vault_list(
    client, auth_headers, db_session
):
    """The count is only useful if it answers for the filters currently on
    screen — a count that ignored them would contradict the list below it."""
    await _hold(
        client, auth_headers, db_session, name="Slabbed", house="psa", grade="10.0"
    )
    await _hold(
        client, auth_headers, db_session, name="Loose", house="loupe", grade="7.0"
    )

    graded = assert_envelope_ok(
        await client.get("/v1/grades/count?graded_only=true", headers=auth_headers)
    )
    assert graded == {"count": 1}

    raw = assert_envelope_ok(
        await client.get("/v1/grades/count?raw_only=true", headers=auth_headers)
    )
    assert raw == {"count": 1}

    high = assert_envelope_ok(
        await client.get("/v1/grades/count?min_grade=9", headers=auth_headers)
    )
    assert high == {"count": 1}

    searched = assert_envelope_ok(
        await client.get("/v1/grades/count?q=slab", headers=auth_headers)
    )
    assert searched == {"count": 1}


@pytest.mark.asyncio
async def test_count_agrees_with_the_rows_the_vault_list_returns(
    client, auth_headers, db_session
):
    """`count` and `/v1/grades` share one filter implementation on purpose: a
    header that says "3 results" above a list of 2 is a bug report waiting to
    happen, so the two must never drift apart."""
    await _hold(client, auth_headers, db_session, name="A", house="psa", grade="10.0")
    await _hold(client, auth_headers, db_session, name="B", house="psa", grade="8.0")
    await _hold(client, auth_headers, db_session, name="C", house="loupe", grade="9.0")

    query = "graded_only=true&min_grade=9"
    rows = assert_envelope_ok(
        await client.get(f"/v1/grades?{query}", headers=auth_headers)
    )
    count = assert_envelope_ok(
        await client.get(f"/v1/grades/count?{query}", headers=auth_headers)
    )
    assert count == {"count": len(rows)} == {"count": 1}


@pytest.mark.asyncio
async def test_count_never_leaks_another_collectors_vault(
    client, auth_headers, second_user_headers, db_session
):
    """Vault size is private: one user's holdings must not move another's
    count, even though both hit the same shared table."""
    await _hold(client, auth_headers, db_session, name="Mine", house="psa", grade="9.0")

    body = assert_envelope_ok(
        await client.get("/v1/grades/count", headers=second_user_headers)
    )
    assert body == {"count": 0}


@pytest.mark.asyncio
async def test_count_requires_auth(client):
    resp = await client.get("/v1/grades/count")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["min_grade=11", "max_grade=-1", "min_value=-5", "q=" + "x" * 121],
)
async def test_count_rejects_impossible_filter_values(client, auth_headers, query):
    """Grades top out at 10 and values can't be negative — rejecting at the
    edge keeps a malformed filter from silently returning zero."""
    resp = await client.get(f"/v1/grades/count?{query}", headers=auth_headers)
    assert_envelope_error(resp, expected_status=422)


# ----------------------------------------------------------------- filters


@pytest.mark.asyncio
async def test_filters_returns_every_option_group_the_vault_bar_renders(
    client, auth_headers
):
    """The filter bar is server-driven so a new grading house or TCG ships
    without an app release — the payload must carry all of its groups."""
    body = assert_envelope_ok(
        await client.get("/v1/grades/filters", headers=auth_headers)
    )
    assert set(body) == {
        "sorts",
        "houses",
        "priceBands",
        "minGrades",
        "maxGrades",
        "tcgs",
    }
    assert {s["key"] for s in body["sorts"]} >= {"recent", "value_desc", "grade_desc"}
    assert {h["key"] for h in body["houses"]} >= {"loupe", "raw", "psa", "bgs", "cgc"}
    assert {t["key"] for t in body["tcgs"]} >= {"all", "pokemon", "magic"}
    # "Any" is first so the bar opens unfiltered.
    assert body["priceBands"][0] == {"label": "Any", "min": None, "max": None}
    assert body["minGrades"][-1] == 10


@pytest.mark.asyncio
async def test_filters_options_are_usable_verbatim_as_query_params(
    client, auth_headers, db_session
):
    """The keys aren't decorative — the client echoes them straight back as
    `house=`/`sort=`, so a label-only value would 400 the next request."""
    await _hold(client, auth_headers, db_session, name="A", house="psa", grade="10.0")
    body = assert_envelope_ok(
        await client.get("/v1/grades/filters", headers=auth_headers)
    )

    for house in (h["key"] for h in body["houses"]):
        resp = await client.get(f"/v1/grades/count?house={house}", headers=auth_headers)
        assert_envelope_ok(resp)
    for sort in (s["key"] for s in body["sorts"]):
        resp = await client.get(f"/v1/grades?sort={sort}", headers=auth_headers)
        assert_envelope_ok(resp)


@pytest.mark.asyncio
async def test_filters_requires_auth(client):
    resp = await client.get("/v1/grades/filters")
    assert resp.status_code in (401, 403)
