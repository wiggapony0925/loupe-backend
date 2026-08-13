"""A NUL in a query parameter must not reach the database.

WHY. ``%00`` in a string query parameter used to travel all the way to
postgres as a bind value, where asyncpg refuses it — ``invalid byte sequence
for encoding "UTF8": 0x00`` — with nothing catching it. Three UNAUTHENTICATED
endpoints returned 500 for a one-character request:

    GET /v1/cards?q=%00
    GET /v1/cards?set_code=%00
    GET /v1/sealed/search?q=%00

Found by fuzzing all 325 operations, not by anyone reading the code, which is
the argument for the fuzzing.

THE TRAP THIS FILE ALSO PINS. ``scope["query_string"]`` is the RAW,
percent-encoded bytes; the ASGI server does not decode it, Starlette does when
it builds ``request.query_params``. The first version of the guard stripped
``b"\\x00"`` and therefore did nothing at all, while looking entirely correct.
So the tests below assert on the encoded form deliberately.
"""

from __future__ import annotations

import pytest

NUL = chr(0)


@pytest.mark.parametrize(
    "path",
    [
        "/v1/cards?q=%00",
        "/v1/cards?set_code=%00",
        "/v1/sealed/search?q=%00",
        # Embedded rather than alone — the strip must not depend on the NUL
        # being the whole value.
        "/v1/cards?q=%00pika",
        "/v1/cards?q=char%00izard",
    ],
)
@pytest.mark.asyncio
async def test_a_nul_in_a_query_param_does_not_crash_the_request(client, path):
    resp = await client.get(path)
    assert resp.status_code != 500, (
        f"{path} returned 500 — a NUL reached the driver again. body={resp.text[:200]}"
    )


@pytest.mark.asyncio
async def test_the_surrounding_value_survives_the_strip(client):
    """Stripping, not rejecting: the rest of the term still searches.

    If this ever starts returning 4xx the guard has been changed to reject,
    which would turn a working request into an error for a character no
    caller means to send.
    """
    clean = await client.get("/v1/cards?q=pika")
    dirty = await client.get("/v1/cards?q=pi%00ka")
    assert clean.status_code == 200
    assert dirty.status_code == 200


def test_the_middleware_strips_the_encoded_form_not_just_the_raw_byte():
    """Directly, because this is the bug the first fix shipped with."""
    from app.http.middleware import StripNulFromQueryStringMiddleware

    captured = {}

    async def app(scope, receive, send):
        captured["query_string"] = scope["query_string"]

    mw = StripNulFromQueryStringMiddleware(app)

    import asyncio

    asyncio.run(mw({"type": "http", "query_string": b"q=char%00izard"}, None, None))
    assert captured["query_string"] == b"q=charizard"

    asyncio.run(
        mw({"type": "http", "query_string": f"q=char{NUL}izard".encode()}, None, None)
    )
    assert captured["query_string"] == b"q=charizard"


def test_a_clean_query_string_is_passed_through_untouched():
    """No copying or rewriting on the normal path."""
    from app.http.middleware import StripNulFromQueryStringMiddleware

    captured = {}

    async def app(scope, receive, send):
        captured["scope"] = scope

    mw = StripNulFromQueryStringMiddleware(app)
    original = {"type": "http", "query_string": b"q=charizard&page=2"}

    import asyncio

    asyncio.run(mw(original, None, None))
    assert captured["scope"] is original
    assert captured["scope"]["query_string"] == b"q=charizard&page=2"
