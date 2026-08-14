"""``/v1/me`` must return the picture the user actually has.

WHAT WAS WRONG. There are two avatar stores and they are not the same thing:

  users.avatar_url          a URL handed over by an OAuth provider at sign-in
  social_profiles.avatar_key  the image the user uploaded themselves

``/v1/me`` served only the first. In production it is empty on all 82 rows —
no OAuth provider ever supplied one — while exactly one account HAS uploaded a
picture. So the one user with a profile photo was told ``avatar_url: null``,
and every screen reading it rendered a blank avatar. The Face ID lock screen
(BiometricLock.tsx:184) is one of them, which is a poor first impression on the
screen you see before you are even signed in.

This was reported twice as "an account has a profile picture and it says
there's no photo url", and dismissed twice as a schema quirk. It was a bug.
"""

from __future__ import annotations

import pytest

from app.auth.jwt import issue_token
from app.social.models import SocialProfile


def _headers(user) -> dict[str, str]:
    token, _ = issue_token(user.id, "access")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_me_returns_the_uploaded_picture(client, db_session, created_user):
    """The regression. An uploaded avatar must reach /v1/me."""
    db_session.add(
        SocialProfile(
            user_id=created_user.id,
            username="pictured",
            avatar_key=f"social/avatars/{created_user.id}",
            avatar_content_type="image/jpeg",
            avatar_version=11,
        )
    )
    await db_session.commit()

    resp = await client.get("/v1/me", headers=_headers(created_user))
    assert resp.status_code == 200
    body = resp.json()["data"]

    assert body["avatar_url"], (
        "the user has an uploaded picture and /v1/me still reports no avatar — "
        "every screen reading this renders a blank"
    )
    assert str(created_user.id) in body["avatar_url"]
    # Version-stamped so a re-upload busts the client cache.
    assert "v=11" in body["avatar_url"]


@pytest.mark.asyncio
async def test_the_uploaded_picture_beats_an_oauth_url(
    client, db_session, created_user
):
    """When both exist, the one the user chose wins.

    The OAuth URL is whatever the provider happened to have on file; the
    uploaded image is a deliberate act.
    """
    created_user.avatar_url = "https://oauth.example.test/photo.jpg"
    db_session.add(
        SocialProfile(
            user_id=created_user.id,
            username="chose_their_own",
            avatar_key=f"social/avatars/{created_user.id}",
            avatar_content_type="image/jpeg",
            avatar_version=3,
        )
    )
    await db_session.commit()

    body = (await client.get("/v1/me", headers=_headers(created_user))).json()["data"]
    assert "oauth.example.test" not in (body["avatar_url"] or "")
    assert str(created_user.id) in body["avatar_url"]


@pytest.mark.asyncio
async def test_an_oauth_url_still_shows_when_nothing_was_uploaded(
    client, db_session, created_user
):
    """The fallback must not be lost. A user who signed in with Google and
    never uploaded anything keeps the provider's picture."""
    created_user.avatar_url = "https://oauth.example.test/photo.jpg"
    await db_session.commit()

    body = (await client.get("/v1/me", headers=_headers(created_user))).json()["data"]
    assert body["avatar_url"] == "https://oauth.example.test/photo.jpg"


@pytest.mark.asyncio
async def test_no_picture_anywhere_is_still_null(client, db_session, created_user):
    """Absent must stay absent — a placeholder string here would make every
    client render a broken image instead of its own fallback."""
    body = (await client.get("/v1/me", headers=_headers(created_user))).json()["data"]
    assert body["avatar_url"] is None


@pytest.mark.asyncio
async def test_a_profile_without_an_upload_does_not_invent_a_url(
    client, db_session, created_user
):
    """Having a social profile is not the same as having a picture."""
    db_session.add(SocialProfile(user_id=created_user.id, username="no_photo"))
    await db_session.commit()

    body = (await client.get("/v1/me", headers=_headers(created_user))).json()["data"]
    assert body["avatar_url"] is None
