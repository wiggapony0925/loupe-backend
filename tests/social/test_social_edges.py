"""Edge scenarios for the social layer: claim-gates, discovery, badges,
case-insensitive handles, and payload deep-link fields."""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.social.test_social_api import _add_graded_card, _claim, _headers


@pytest.mark.asyncio
async def test_follow_requires_claimed_profile(client, created_user, second_user):
    """A viewer with no handle can't enter the graph (counts vs lists drift)."""
    await _claim(client, second_user, "target")
    resp = await client.post(
        "/v1/social/users/target/follow", headers=_headers(created_user)
    )
    assert_envelope_error(resp, expected_status=409)

    # After claiming, the same follow goes through.
    await _claim(client, created_user, "nowclaimed")
    resp = await client.post(
        "/v1/social/users/target/follow", headers=_headers(created_user)
    )
    assert assert_envelope_ok(resp)["relationship"] == "following"


@pytest.mark.asyncio
async def test_profile_lookup_is_case_insensitive(client, created_user, second_user):
    await _claim(client, created_user, "CamelCase")
    resp = await client.get("/v1/social/users/CAMELCASE", headers=_headers(second_user))
    assert assert_envelope_ok(resp)["username"] == "camelcase"


@pytest.mark.asyncio
async def test_suggested_excludes_followed_and_self(
    client, created_user, second_user, db_session
):
    await _claim(client, created_user, "me_myself")
    await _claim(client, second_user, "fresh_face")

    resp = await client.get("/v1/social/suggested", headers=_headers(created_user))
    names = [r["username"] for r in assert_envelope_ok(resp)]
    assert "fresh_face" in names
    assert "me_myself" not in names  # never suggest yourself

    # Following them removes them from suggestions.
    await client.post(
        "/v1/social/users/fresh_face/follow", headers=_headers(created_user)
    )
    resp = await client.get("/v1/social/suggested", headers=_headers(created_user))
    assert "fresh_face" not in [r["username"] for r in assert_envelope_ok(resp)]


@pytest.mark.asyncio
async def test_suggested_includes_private_requested_excluded(
    client, created_user, second_user
):
    await _claim(client, created_user, "asker2")
    await _claim(client, second_user, "shyguy", is_private=True)

    # Private profiles are suggestable (following becomes a request)…
    resp = await client.get("/v1/social/suggested", headers=_headers(created_user))
    assert "shyguy" in [r["username"] for r in assert_envelope_ok(resp)]

    # …until a request is pending.
    await client.post("/v1/social/users/shyguy/follow", headers=_headers(created_user))
    resp = await client.get("/v1/social/suggested", headers=_headers(created_user))
    assert "shyguy" not in [r["username"] for r in assert_envelope_ok(resp)]


@pytest.mark.asyncio
async def test_pro_badge_reflects_raw_plan(
    client, created_user, second_user, db_session
):
    await _claim(client, created_user, "probadge")
    await _claim(client, second_user, "viewer2")
    created_user.plan = "pro"
    await db_session.commit()

    resp = await client.get("/v1/social/users/probadge", headers=_headers(second_user))
    assert assert_envelope_ok(resp)["is_pro"] is True

    resp = await client.get(
        "/v1/social/search", params={"q": "probadge"}, headers=_headers(second_user)
    )
    assert assert_envelope_ok(resp)[0]["is_pro"] is True


@pytest.mark.asyncio
async def test_collection_items_carry_card_id(
    client, created_user, second_user, db_session
):
    await _claim(client, created_user, "linker")
    await _claim(client, second_user, "viewer3")
    row = await _add_graded_card(db_session, created_user)

    resp = await client.get(
        "/v1/social/users/linker/collection", headers=_headers(second_user)
    )
    items = assert_envelope_ok(resp)["items"]
    assert items[0]["card_id"] == str(row.card_id)


@pytest.mark.asyncio
async def test_unfollow_when_not_following_is_noop(client, created_user, second_user):
    await _claim(client, created_user, "calm")
    await _claim(client, second_user, "stranger2")
    resp = await client.delete(
        "/v1/social/users/stranger2/follow", headers=_headers(created_user)
    )
    assert assert_envelope_ok(resp)["relationship"] == "none"
