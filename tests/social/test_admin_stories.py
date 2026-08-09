"""The portal's story dev-tools: seed on demand, force-expire, retrigger.

The 24-hour lifecycle is the one part of stories nobody can test by
waiting, so these are the levers that close the loop — and the contract
worth pinning is the retrigger rule: seeding skips accounts with a live
story, and works again the moment theirs expires.
"""

from __future__ import annotations

import io
import struct
import zlib
from uuid import uuid4

import pytest

from app.auth.jwt import issue_token
from app.models.user import User
from tests.conftest import assert_envelope_ok


async def _make_admin(db) -> User:
    """DB-flag admin, made in the test body — same pattern as the other
    admin suites, dodging a fixture-ordering quirk between `client` and
    the in-memory engine."""
    user = User(email=f"admin+{uuid4().hex[:8]}@example.com", is_admin=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _headers(user) -> dict[str, str]:
    token, _ = issue_token(user.id, "access")
    return {"Authorization": f"Bearer {token}"}


def _png(width: int = 10, height: int = 14) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\xff\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def _author_with_posts(client, user, handle: str, posts: int = 3) -> None:
    assert_envelope_ok(
        await client.put(
            "/v1/social/me", json={"username": handle}, headers=_headers(user)
        )
    )
    for i in range(posts):
        resp = await client.post(
            "/v1/social/posts",
            data={"body": f"post {i} #seeded"},
            files={"images": ("p.png", io.BytesIO(_png()), "image/png")},
            headers=_headers(user),
        )
        assert_envelope_ok(resp, expected_status=201)


@pytest.mark.asyncio
async def test_dev_tools_are_admin_only(client, created_user):
    for method, path in [
        ("GET", "/v1/admin/social/stories"),
        ("POST", "/v1/admin/social/stories/seed"),
    ]:
        resp = await client.request(method, path, headers=_headers(created_user))
        assert resp.status_code == 403, path


@pytest.mark.asyncio
async def test_seed_makes_accounts_post_and_varies_the_counts(
    client, db_session, created_user, second_user
):
    admin_user = await _make_admin(db_session)
    await _author_with_posts(client, created_user, "seedauthor1", posts=3)
    await _author_with_posts(client, second_user, "seedauthor2", posts=2)

    result = assert_envelope_ok(
        await client.post("/v1/admin/social/stories/seed", headers=_headers(admin_user))
    )
    assert result["created"] >= 3
    assert set(result["authors"]) == {"seedauthor1", "seedauthor2"}

    rows = assert_envelope_ok(
        await client.get("/v1/admin/social/stories", headers=_headers(admin_user))
    )
    live = [r for r in rows if r["live"]]
    per_author: dict[str, int] = {}
    for row in live:
        per_author[row["username"]] = per_author.get(row["username"], 0) + 1
    # The user's requirement, verbatim: some accounts post MORE THAN ONE.
    assert max(per_author.values()) >= 2

    # …and the stories are real: the tray shows them to a follower.
    await client.post(
        "/v1/social/users/seedauthor2/follow", headers=_headers(created_user)
    )
    tray = assert_envelope_ok(
        await client.get("/v1/social/stories/tray", headers=_headers(created_user))
    )
    assert "seedauthor2" in [e["author"]["username"] for e in tray["entries"]]


@pytest.mark.asyncio
async def test_seeding_skips_live_accounts_and_retriggers_after_expiry(
    client, db_session, created_user
):
    admin_user = await _make_admin(db_session)
    """The retrigger loop: no-op while a story is live, works again once
    it has been expired — which is exactly how the 24h clock behaves,
    minus the waiting."""
    await _author_with_posts(client, created_user, "retrigger1", posts=2)

    first = assert_envelope_ok(
        await client.post("/v1/admin/social/stories/seed", headers=_headers(admin_user))
    )
    assert first["created"] >= 1

    again = assert_envelope_ok(
        await client.post("/v1/admin/social/stories/seed", headers=_headers(admin_user))
    )
    assert again["created"] == 0
    assert again["skipped_live"] >= 1

    rows = assert_envelope_ok(
        await client.get("/v1/admin/social/stories", headers=_headers(admin_user))
    )
    for row in [r for r in rows if r["live"]]:
        assert_envelope_ok(
            await client.post(
                f"/v1/admin/social/stories/{row['id']}/expire",
                headers=_headers(admin_user),
            )
        )

    third = assert_envelope_ok(
        await client.post("/v1/admin/social/stories/seed", headers=_headers(admin_user))
    )
    assert third["created"] >= 1


@pytest.mark.asyncio
async def test_force_expire_pulls_the_story_from_live_surfaces_but_not_the_archive(
    client, db_session, created_user, second_user
):
    admin_user = await _make_admin(db_session)
    """The button exists to PROVE the lifecycle. An expired story must leave
    the tray immediately and still sit in its author's archive."""
    await _author_with_posts(client, created_user, "expireme1", posts=1)
    assert_envelope_ok(
        await client.put(
            "/v1/social/me",
            json={"username": "watcher1"},
            headers=_headers(second_user),
        )
    )
    await client.post(
        "/v1/social/users/expireme1/follow", headers=_headers(second_user)
    )

    assert_envelope_ok(
        await client.post("/v1/admin/social/stories/seed", headers=_headers(admin_user))
    )
    tray = assert_envelope_ok(
        await client.get("/v1/social/stories/tray", headers=_headers(second_user))
    )
    assert "expireme1" in [e["author"]["username"] for e in tray["entries"]]

    rows = assert_envelope_ok(
        await client.get("/v1/admin/social/stories", headers=_headers(admin_user))
    )
    for row in rows:
        if row["username"] == "expireme1" and row["live"]:
            expired = assert_envelope_ok(
                await client.post(
                    f"/v1/admin/social/stories/{row['id']}/expire",
                    headers=_headers(admin_user),
                )
            )
            assert expired["live"] is False

    tray = assert_envelope_ok(
        await client.get("/v1/social/stories/tray", headers=_headers(second_user))
    )
    assert "expireme1" not in [e["author"]["username"] for e in tray["entries"]]

    archive = assert_envelope_ok(
        await client.get("/v1/social/stories/archive", headers=_headers(created_user))
    )
    assert len(archive) >= 1
