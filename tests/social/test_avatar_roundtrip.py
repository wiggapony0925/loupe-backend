"""Upload a profile picture, then fetch it back.

Reported repeatedly: "it won't save my profile picture". The pieces were
each verified in isolation (the blob lands in GCS, the endpoint serves 200
for one user) without ever proving the whole loop. This walks it end to end
with real image bytes: POST the avatar, read the profile, follow the URL it
advertises, and check the bytes that come back are the ones sent.
"""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_ok
from tests.social.test_social_api import _claim, _headers

#: The service stores and serves bytes verbatim (no decode, no resize), so
#: the round trip is proven by any recognisable payload. A JPEG magic number
#: plus filler keeps it honest about what it claims to be.
TINY_JPEG = b"\xff\xd8\xff\xe0" + b"loupe-avatar-test" * 8 + b"\xff\xd9"


@pytest.mark.asyncio
async def test_uploading_an_avatar_then_fetching_it_returns_the_same_bytes(
    client, created_user
):
    await _claim(client, created_user, "shutterbug")

    resp = await client.post(
        "/v1/social/me/avatar",
        headers=_headers(created_user),
        files={"image": ("me.jpg", TINY_JPEG, "image/jpeg")},
    )
    profile = assert_envelope_ok(resp)

    # 1. The profile now advertises a URL…
    url = profile.get("avatar_url")
    assert url, "upload succeeded but the profile still has no avatar_url"
    assert "?v=" in url, "the URL must be cache-busted or clients show the old one"

    # 2. …/social/me agrees (this is what the settings screen reads).
    me = assert_envelope_ok(
        await client.get("/v1/social/me", headers=_headers(created_user))
    )
    assert me["profile"]["avatar_url"] == url

    # 3. …and following it returns the bytes we sent.
    img = await client.get(url)
    assert img.status_code == 200, f"the advertised URL 404s: {url}"
    assert img.content == TINY_JPEG
    assert img.headers["content-type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_a_second_upload_changes_the_url_so_clients_refetch(client, created_user):
    """Same key, new version. Without the bump every client keeps showing the
    old picture from cache — which is exactly "it didn't save"."""
    await _claim(client, created_user, "shutterbug")
    first = assert_envelope_ok(
        await client.post(
            "/v1/social/me/avatar",
            headers=_headers(created_user),
            files={"image": ("a.jpg", TINY_JPEG, "image/jpeg")},
        )
    )["avatar_url"]

    second = assert_envelope_ok(
        await client.post(
            "/v1/social/me/avatar",
            headers=_headers(created_user),
            files={"image": ("b.jpg", TINY_JPEG + b"\x00", "image/jpeg")},
        )
    )["avatar_url"]

    assert first != second, "the cache-busting version did not advance"
    assert (await client.get(second)).status_code == 200


@pytest.mark.asyncio
async def test_the_avatar_survives_a_profile_save(client, created_user):
    """Editing bio/location must not drop the picture — the settings screen
    saves the whole profile after an upload."""
    await _claim(client, created_user, "shutterbug")
    url = assert_envelope_ok(
        await client.post(
            "/v1/social/me/avatar",
            headers=_headers(created_user),
            files={"image": ("me.jpg", TINY_JPEG, "image/jpeg")},
        )
    )["avatar_url"]

    saved = assert_envelope_ok(
        await client.put(
            "/v1/social/me",
            headers=_headers(created_user),
            json={"username": "shutterbug", "bio": "collector", "is_private": False},
        )
    )
    assert saved["avatar_url"] == url, "saving the profile wiped the picture"
    assert (await client.get(url)).status_code == 200
