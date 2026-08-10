"""Per-platform social links on collector profiles.

Pins the write-side contract (canonicalisation, the allowlist, the scheme
gate) and the read surfaces (own profile, public view) — plus the update
semantics: an upsert that omits ``links`` must not wipe them, ``{}`` is the
explicit clear.
"""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.social.test_social_api import _claim, _headers

# ── Canonicalisation ──


@pytest.mark.asyncio
async def test_links_roundtrip_canonicalized(client, created_user):
    data = await _claim(
        client,
        created_user,
        "lisacollects",
        links={
            "instagram": "@lisacollects",
            "web": "lisacollects.com",
            "youtube": "https://youtube.com/@lisacollects",
        },
    )
    assert data["links"] == {
        "instagram": "https://instagram.com/lisacollects",
        "web": "https://lisacollects.com",
        "youtube": "https://youtube.com/@lisacollects",
    }

    # And the same shape comes back on GET /me.
    resp = await client.get("/v1/social/me", headers=_headers(created_user))
    me = assert_envelope_ok(resp)
    assert me["profile"]["links"]["instagram"] == "https://instagram.com/lisacollects"


@pytest.mark.asyncio
async def test_bare_handle_without_at_sign_expands_too(client, created_user):
    data = await _claim(client, created_user, "handles", links={"x": "lisacollects"})
    assert data["links"] == {"x": "https://x.com/lisacollects"}


# ── Rejections ──


@pytest.mark.asyncio
async def test_unknown_platform_rejected(client, created_user):
    resp = await client.put(
        "/v1/social/me",
        json={"username": "unknownkey", "links": {"myspace": "https://myspace.com/x"}},
        headers=_headers(created_user),
    )
    err = assert_envelope_error(resp, expected_status=422)
    assert "myspace" in err["message"]


@pytest.mark.asyncio
async def test_javascript_scheme_rejected(client, created_user):
    resp = await client.put(
        "/v1/social/me",
        json={"username": "xssguy", "links": {"web": "javascript:alert(1)"}},
        headers=_headers(created_user),
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_bad_handle_chars_rejected(client, created_user):
    resp = await client.put(
        "/v1/social/me",
        json={"username": "badhandle", "links": {"instagram": "@lisa collects!"}},
        headers=_headers(created_user),
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_overlong_link_rejected(client, created_user):
    resp = await client.put(
        "/v1/social/me",
        json={
            "username": "longlink",
            "links": {"web": "https://example.com/" + "a" * 300},
        },
        headers=_headers(created_user),
    )
    assert_envelope_error(resp, expected_status=422)


# ── Public visibility ──


@pytest.mark.asyncio
async def test_links_show_on_public_profile_view(client, created_user, second_user):
    await _claim(client, created_user, "linkedup", links={"instagram": "@linkedup"})
    await _claim(client, second_user, "onlooker")

    resp = await client.get("/v1/social/users/linkedup", headers=_headers(second_user))
    view = assert_envelope_ok(resp)
    assert view["links"] == {"instagram": "https://instagram.com/linkedup"}


# ── Update semantics ──


@pytest.mark.asyncio
async def test_empty_string_value_removes_the_entry(client, created_user):
    await _claim(
        client,
        created_user,
        "pruner",
        links={"instagram": "@pruner", "web": "pruner.com"},
    )
    data = await _claim(
        client, created_user, "pruner", links={"instagram": "", "web": "pruner.com"}
    )
    assert data["links"] == {"web": "https://pruner.com"}


@pytest.mark.asyncio
async def test_null_links_on_claim_leaves_profile_with_none(client, created_user):
    data = await _claim(client, created_user, "linkless", links=None)
    assert data["links"] is None


@pytest.mark.asyncio
async def test_upsert_without_links_key_preserves_existing(client, created_user):
    await _claim(client, created_user, "keeper", links={"instagram": "@keeper"})
    # A client that predates the feature re-submits the settings form
    # without the field — the links must survive that.
    data = await _claim(client, created_user, "keeper", bio="still here")
    assert data["links"] == {"instagram": "https://instagram.com/keeper"}


@pytest.mark.asyncio
async def test_empty_dict_explicitly_clears_links(client, created_user):
    await _claim(client, created_user, "clearer", links={"instagram": "@clearer"})
    data = await _claim(client, created_user, "clearer", links={})
    assert data["links"] is None
