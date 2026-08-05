"""Likes and unique-viewer counts on a collector's profile.

The numbers a collector sees about themselves have to be defensible, so the
rules pinned here are the ones that keep them honest: you cannot inflate your
own stats, a like is idempotent rather than a counter you can pump, and a
repeat visit is the same visitor — not a new one.
"""

from __future__ import annotations

import pytest

from app.auth.jwt import issue_token
from tests.conftest import assert_envelope_error, assert_envelope_ok


def _headers(user) -> dict[str, str]:
    token, _ = issue_token(user.id, "access")
    return {"Authorization": f"Bearer {token}"}


async def _claim(client, user, username: str, **extra) -> dict:
    resp = await client.put(
        "/v1/social/me",
        json={"username": username, **extra},
        headers=_headers(user),
    )
    return assert_envelope_ok(resp)


async def _view(client, viewer, username: str) -> dict:
    resp = await client.get(f"/v1/social/users/{username}", headers=_headers(viewer))
    return assert_envelope_ok(resp)


# ── Likes ──


@pytest.mark.anyio
async def test_like_is_counted_and_reflected_back(client, created_user, second_user):
    await _claim(client, created_user, "ash")
    await _claim(client, second_user, "misty")

    resp = await client.post("/v1/social/users/ash/like", headers=_headers(second_user))
    body = assert_envelope_ok(resp)
    assert body["liked"] is True
    assert body["like_count"] == 1

    # The profile agrees, and knows it was *this* viewer who liked it.
    profile = await _view(client, second_user, "ash")
    assert profile["like_count"] == 1
    assert profile["viewer_has_liked"] is True


@pytest.mark.anyio
async def test_liking_twice_does_not_double_count(client, created_user, second_user):
    await _claim(client, created_user, "ash")
    await _claim(client, second_user, "misty")

    await client.post("/v1/social/users/ash/like", headers=_headers(second_user))
    resp = await client.post("/v1/social/users/ash/like", headers=_headers(second_user))

    # Idempotent: a double-tap is not two likes.
    assert assert_envelope_ok(resp)["like_count"] == 1


@pytest.mark.anyio
async def test_unlike_removes_it_and_is_idempotent(client, created_user, second_user):
    await _claim(client, created_user, "ash")
    await _claim(client, second_user, "misty")
    await client.post("/v1/social/users/ash/like", headers=_headers(second_user))

    resp = await client.delete(
        "/v1/social/users/ash/like", headers=_headers(second_user)
    )
    body = assert_envelope_ok(resp)
    assert body["liked"] is False
    assert body["like_count"] == 0

    # Unliking what you never liked is a no-op, not an error.
    resp = await client.delete(
        "/v1/social/users/ash/like", headers=_headers(second_user)
    )
    assert assert_envelope_ok(resp)["like_count"] == 0


@pytest.mark.anyio
async def test_cannot_like_your_own_profile(client, created_user):
    await _claim(client, created_user, "ash")
    resp = await client.post("/v1/social/users/ash/like", headers=_headers(created_user))
    # A stat you can raise by yourself is not a stat.
    assert_envelope_error(resp, expected_status=400)


# ── Views ──


@pytest.mark.anyio
async def test_viewing_a_profile_counts_the_viewer(client, created_user, second_user):
    await _claim(client, created_user, "ash")
    await _claim(client, second_user, "misty")

    profile = await _view(client, second_user, "ash")
    # Counted immediately — a figure that lags one page load behind reads
    # as broken to the person refreshing their own profile.
    assert profile["view_count"] == 1


@pytest.mark.anyio
async def test_repeat_visits_are_the_same_viewer(client, created_user, second_user):
    await _claim(client, created_user, "ash")
    await _claim(client, second_user, "misty")

    await _view(client, second_user, "ash")
    await _view(client, second_user, "ash")
    profile = await _view(client, second_user, "ash")

    # Unique viewers, not hits: refreshing does not manufacture an audience.
    assert profile["view_count"] == 1


@pytest.mark.anyio
async def test_your_own_visits_are_not_counted(client, created_user):
    await _claim(client, created_user, "ash")

    await _view(client, created_user, "ash")
    profile = await _view(client, created_user, "ash")

    assert profile["view_count"] == 0
    # And you are never shown as having liked yourself.
    assert profile["viewer_has_liked"] is False


@pytest.mark.anyio
async def test_distinct_viewers_accumulate(
    client, created_user, second_user, db_session
):
    from tests.factories import make_user

    await _claim(client, created_user, "ash")
    await _claim(client, second_user, "misty")
    third = await make_user(db_session)
    await _claim(client, third, "brock")

    await _view(client, second_user, "ash")
    profile = await _view(client, third, "ash")

    assert profile["view_count"] == 2
