"""End-to-end tests for the social layer (``/v1/social``).

Covers the Instagram semantics the module promises: claimable usernames,
public vs private follows, the request inbox, privacy-gated collections,
and the profile-picture round-trip.
"""

from __future__ import annotations

import io
import uuid
from decimal import Decimal
from urllib.parse import quote

import pytest

from app.auth.jwt import issue_token
from app.models.card import Card
from app.models.grade import GradedCard
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_card, make_user


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


async def _add_graded_card(db, user, value: str | None = "150.00") -> GradedCard:
    card = await make_card(db, name=f"Card {uuid.uuid4().hex[:6]}")
    row = GradedCard(
        user_id=user.id,
        card_id=card.id,
        grade=Decimal("9.5"),
        estimated_value_usd=Decimal(value) if value is not None else None,
        purchase_price_usd=Decimal("40.00"),
        notes="secret cost basis",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# ── Profile claim + search ──


@pytest.mark.asyncio
async def test_claim_profile_and_me_roundtrip(client, created_user, auth_headers):
    resp = await client.get("/v1/social/me", headers=auth_headers)
    me = assert_envelope_ok(resp)
    assert me["profile"] is None

    data = await _claim(
        client, created_user, "JeffCollects", bio="PSA 10 hunter", location="Miami, FL"
    )
    assert data["username"] == "jeffcollects"  # stored lowercase
    assert data["bio"] == "PSA 10 hunter"
    assert data["location"] == "Miami, FL"
    assert data["is_private"] is False

    resp = await client.get("/v1/social/me", headers=auth_headers)
    me = assert_envelope_ok(resp)
    assert me["profile"]["username"] == "jeffcollects"


@pytest.mark.asyncio
async def test_username_taken_and_reserved(client, created_user, second_user):
    await _claim(client, created_user, "collector1")

    resp = await client.put(
        "/v1/social/me",
        json={"username": "Collector1"},
        headers=_headers(second_user),
    )
    assert_envelope_error(resp, expected_status=409)

    resp = await client.put(
        "/v1/social/me",
        json={"username": "admin"},
        headers=_headers(second_user),
    )
    assert_envelope_error(resp, expected_status=409)


@pytest.mark.asyncio
async def test_search_finds_by_handle_and_name(client, created_user, second_user):
    await _claim(client, created_user, "pikapulls")
    await _claim(client, second_user, "vaultboy")

    resp = await client.get(
        "/v1/social/search",
        params={"q": "pika"},
        headers=_headers(second_user),
    )
    rows = assert_envelope_ok(resp)
    assert [r["username"] for r in rows] == ["pikapulls"]
    assert rows[0]["relationship"] == "none"

    # Display-name match ("Other" is second_user's factory display name).
    resp = await client.get(
        "/v1/social/search",
        params={"q": "other"},
        headers=_headers(created_user),
    )
    rows = assert_envelope_ok(resp)
    assert [r["username"] for r in rows] == ["vaultboy"]


# ── Public follow flow ──


@pytest.mark.asyncio
async def test_follow_public_account(client, created_user, second_user):
    await _claim(client, created_user, "alice")
    await _claim(client, second_user, "bob")

    resp = await client.post(
        "/v1/social/users/bob/follow", headers=_headers(created_user)
    )
    assert assert_envelope_ok(resp)["relationship"] == "following"

    # Idempotent.
    resp = await client.post(
        "/v1/social/users/bob/follow", headers=_headers(created_user)
    )
    assert assert_envelope_ok(resp)["relationship"] == "following"

    resp = await client.get("/v1/social/users/bob", headers=_headers(created_user))
    view = assert_envelope_ok(resp)
    assert view["follower_count"] == 1
    assert view["relationship"] == "following"
    assert view["can_view_collection"] is True

    resp = await client.get(
        "/v1/social/users/bob/followers", headers=_headers(second_user)
    )
    rows = assert_envelope_ok(resp)
    assert [r["username"] for r in rows] == ["alice"]

    # Unfollow → back to none.
    resp = await client.delete(
        "/v1/social/users/bob/follow", headers=_headers(created_user)
    )
    assert assert_envelope_ok(resp)["relationship"] == "none"


@pytest.mark.asyncio
async def test_cannot_follow_yourself(client, created_user):
    await _claim(client, created_user, "loner")
    resp = await client.post(
        "/v1/social/users/loner/follow", headers=_headers(created_user)
    )
    assert_envelope_error(resp, expected_status=400)


# ── Private accounts + requests ──


@pytest.mark.asyncio
async def test_private_account_request_accept_flow(client, created_user, second_user):
    await _claim(client, created_user, "seeker")
    await _claim(client, second_user, "hermit", is_private=True)

    # Follow becomes a pending request.
    resp = await client.post(
        "/v1/social/users/hermit/follow", headers=_headers(created_user)
    )
    assert assert_envelope_ok(resp)["relationship"] == "requested"

    # Collection stays hidden while pending.
    resp = await client.get(
        "/v1/social/users/hermit/collection", headers=_headers(created_user)
    )
    assert_envelope_error(resp, expected_status=403)

    # The owner sees the request and accepts it.
    resp = await client.get("/v1/social/requests", headers=_headers(second_user))
    reqs = assert_envelope_ok(resp)
    assert len(reqs) == 1
    assert reqs[0]["requester"]["username"] == "seeker"

    resp = await client.post(
        f"/v1/social/requests/{reqs[0]['id']}/accept",
        headers=_headers(second_user),
    )
    assert resp.status_code == 204

    resp = await client.get("/v1/social/users/hermit", headers=_headers(created_user))
    view = assert_envelope_ok(resp)
    assert view["relationship"] == "following"
    assert view["can_view_collection"] is True


@pytest.mark.asyncio
async def test_decline_and_cancel_request(client, created_user, second_user):
    await _claim(client, created_user, "asker")
    await _claim(client, second_user, "gatekeeper", is_private=True)

    await client.post(
        "/v1/social/users/gatekeeper/follow", headers=_headers(created_user)
    )

    # Requester can cancel (DELETE follow while pending).
    resp = await client.delete(
        "/v1/social/users/gatekeeper/follow", headers=_headers(created_user)
    )
    assert assert_envelope_ok(resp)["relationship"] == "none"
    resp = await client.get("/v1/social/requests", headers=_headers(second_user))
    assert assert_envelope_ok(resp) == []

    # Ask again; this time the owner declines.
    await client.post(
        "/v1/social/users/gatekeeper/follow", headers=_headers(created_user)
    )
    reqs = assert_envelope_ok(
        await client.get("/v1/social/requests", headers=_headers(second_user))
    )
    resp = await client.post(
        f"/v1/social/requests/{reqs[0]['id']}/decline",
        headers=_headers(second_user),
    )
    assert resp.status_code == 204
    resp = await client.get(
        "/v1/social/users/gatekeeper", headers=_headers(created_user)
    )
    assert assert_envelope_ok(resp)["relationship"] == "none"


@pytest.mark.asyncio
async def test_going_public_accepts_pending_requests(client, created_user, second_user):
    await _claim(client, created_user, "waiting")
    await _claim(client, second_user, "flipflop", is_private=True)

    await client.post(
        "/v1/social/users/flipflop/follow", headers=_headers(created_user)
    )

    # Owner switches to public → the pending request becomes a follow.
    await _claim(client, second_user, "flipflop", is_private=False)

    resp = await client.get("/v1/social/users/flipflop", headers=_headers(created_user))
    view = assert_envelope_ok(resp)
    assert view["relationship"] == "following"
    assert view["follower_count"] == 1


# ── The shared collection ──


@pytest.mark.asyncio
async def test_collection_view_hides_cost_basis(
    client, db_session, created_user, second_user
):
    await _claim(client, created_user, "viewer")
    await _claim(client, second_user, "showcase")
    await _add_graded_card(db_session, second_user, "150.00")
    await _add_graded_card(db_session, second_user, "50.00")

    resp = await client.get(
        "/v1/social/users/showcase/collection", headers=_headers(created_user)
    )
    data = assert_envelope_ok(resp)
    assert data["total_cards"] == 2
    assert float(data["estimated_value_usd"]) == 200.0
    assert len(data["items"]) == 2
    # Highest value first, and no cost-basis / notes leakage.
    assert float(data["items"][0]["estimated_value_usd"]) == 150.0
    for item in data["items"]:
        assert "purchase_price_usd" not in item
        assert "notes" not in item


@pytest.mark.asyncio
async def test_private_collection_requires_follow(
    client, db_session, created_user, second_user
):
    await _claim(client, created_user, "stranger")
    await _claim(client, second_user, "fortress", is_private=True)
    await _add_graded_card(db_session, second_user)

    resp = await client.get(
        "/v1/social/users/fortress/collection", headers=_headers(created_user)
    )
    assert_envelope_error(resp, expected_status=403)

    # The profile header itself stays visible (counts, no content).
    resp = await client.get("/v1/social/users/fortress", headers=_headers(created_user))
    view = assert_envelope_ok(resp)
    assert view["is_private"] is True
    assert view["card_count"] == 1
    assert view["can_view_collection"] is False


# ── Profile pictures ──


@pytest.mark.asyncio
async def test_avatar_upload_and_public_serving(client, created_user, auth_headers):
    await _claim(client, created_user, "hasface")

    fake_jpeg = b"\xff\xd8\xff\xe0" + b"x" * 128
    resp = await client.post(
        "/v1/social/me/avatar",
        files={"image": ("me.jpg", io.BytesIO(fake_jpeg), "image/jpeg")},
        headers=auth_headers,
    )
    data = assert_envelope_ok(resp)
    assert data["avatar_url"] and "?v=1" in data["avatar_url"]

    # Served publicly (no auth) with long-cache headers.
    resp = await client.get(f"/v1/social/avatar/{created_user.id}")
    assert resp.status_code == 200
    assert resp.content == fake_jpeg
    assert "immutable" in resp.headers["cache-control"]


@pytest.mark.asyncio
async def test_avatar_requires_claimed_profile_and_image_type(
    client, created_user, auth_headers
):
    resp = await client.post(
        "/v1/social/me/avatar",
        files={"image": ("me.jpg", io.BytesIO(b"data"), "image/jpeg")},
        headers=auth_headers,
    )
    assert_envelope_error(resp, expected_status=409)

    await _claim(client, created_user, "typed")
    resp = await client.post(
        "/v1/social/me/avatar",
        files={"image": ("evil.html", io.BytesIO(b"<html>"), "text/html")},
        headers=auth_headers,
    )
    assert_envelope_error(resp, expected_status=415)


# ── Auth boundary ──


@pytest.mark.asyncio
async def test_social_requires_auth(client):
    for path in ("/v1/social/me", "/v1/social/requests", "/v1/social/users/anyone"):
        resp = await client.get(path)
        assert resp.status_code == 401, path


# ── Row trend (the sparkline on a profile's card rows) ──


@pytest.mark.asyncio
async def test_collection_rows_carry_the_price_trend(
    client, db_session, created_user, second_user
):
    """A card row on a profile shows the SAME trend as the owner's vault.

    The line is built from the card's real price history through the vault's
    own `spark_series`, so the two surfaces cannot drift apart.
    """
    await _claim(client, created_user, "viewer")
    await _claim(client, second_user, "trendy")

    card = await make_card(db_session, name="Trending Charizard")
    card.card_metadata = {
        "price_history": [
            {"date": "2026-01-01", "priceUsd": 100.0},
            {"date": "2026-02-01", "priceUsd": 150.0},
            {"date": "2026-03-01", "priceUsd": 200.0},
        ]
    }
    db_session.add(
        GradedCard(
            user_id=second_user.id,
            card_id=card.id,
            grade=Decimal("10"),
            estimated_value_usd=Decimal("200.00"),
        )
    )
    await db_session.commit()

    resp = await client.get(
        "/v1/social/users/trendy/collection", headers=_headers(created_user)
    )
    item = assert_envelope_ok(resp)["items"][0]

    assert len(item["spark_points"]) == 14, "clients draw a fixed-width line"
    assert item["spark_points"][0] == 100.0
    assert item["spark_points"][-1] == 200.0
    # 100 → 200 is +100%.
    assert item["spark_delta_pct"] == 100.0


@pytest.mark.asyncio
async def test_a_card_with_no_history_gets_a_flat_line_not_invented_motion(
    client, db_session, created_user, second_user
):
    await _claim(client, created_user, "viewer")
    await _claim(client, second_user, "quiet")
    await _add_graded_card(db_session, second_user, "75.00")

    resp = await client.get(
        "/v1/social/users/quiet/collection", headers=_headers(created_user)
    )
    item = assert_envelope_ok(resp)["items"][0]

    assert set(item["spark_points"]) == {75.0}, "flat at the current estimate"
    assert item["spark_delta_pct"] == 0.0


# ── Collection peek on directory rows ──


@pytest.mark.asyncio
async def test_discover_rows_show_what_the_collector_owns(
    client, db_session, created_user, second_user
):
    """A collector directory on a CARD app has to show cards.

    Without this the Community page is a list of names and avatars — the
    reason it read as empty.
    """
    await _claim(client, created_user, "browser")
    await _claim(client, second_user, "bigvault")
    for value in ("500.00", "300.00", "100.00", "50.00"):
        grade = await _add_graded_card(db_session, second_user, value)
        # Art is what the peek shows; a card with no image is skipped.
        card = await db_session.get(Card, grade.card_id)
        card.image_url = f"https://img.example/{value}.png"
    await db_session.commit()

    resp = await client.get("/v1/social/discover", headers=_headers(created_user))
    rows = assert_envelope_ok(resp)["featured"]
    row = next(r for r in rows if r["username"] == "bigvault")

    assert row["card_count"] == 4
    # Their best cards, capped — a peek, not the whole vault.
    assert len(row["preview_image_urls"]) == 3


@pytest.mark.asyncio
async def test_a_private_collection_never_leaks_its_size_to_strangers(
    client, db_session, created_user, second_user
):
    """How much someone owns is part of what a private account is hiding."""
    await _claim(client, created_user, "stranger")
    await _claim(client, second_user, "secretive", is_private=True)
    await _add_graded_card(db_session, second_user, "9000.00")

    resp = await client.get("/v1/social/discover", headers=_headers(created_user))
    data = assert_envelope_ok(resp)
    row = next(
        r for r in [*data["featured"], *data["more"]] if r["username"] == "secretive"
    )

    assert row["card_count"] == 0
    assert row["preview_image_urls"] == []


@pytest.mark.asyncio
async def test_the_peek_costs_a_FIXED_number_of_queries(db_session, created_user):
    """A page of rows must not cost two queries per row.

    N+1 here only hurts once a real directory exists, so the query count is
    asserted rather than eyeballed. Counting statements on the live session
    tests the behaviour, not the implementation.
    """
    from sqlalchemy import event

    from app.social.services._common import collection_peeks

    users = [created_user]
    for _ in range(4):
        users.append(await make_user(db_session))
    for u in users:
        await _add_graded_card(db_session, u, "100.00")

    statements: list[str] = []
    # The test session binds a sync Engine directly.
    sync_engine = db_session.get_bind()

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(sync_engine, "before_cursor_execute", record)
    try:
        peeks = await collection_peeks(db_session, [u.id for u in users])
    finally:
        event.remove(sync_engine, "before_cursor_execute", record)

    assert len(peeks) == 5
    assert all(peeks[u.id].count == 1 for u in users)
    # Count + art. Five collectors must not cost ten round trips.
    assert len(statements) == 2, statements


# ── Explore grid + identifier search ──


@pytest.mark.asyncio
async def test_explore_returns_card_art_from_public_collections(
    client, db_session, created_user, second_user
):
    await _claim(client, created_user, "browser")
    await _claim(client, second_user, "sharer")
    for value in ("300.00", "200.00"):
        grade = await _add_graded_card(db_session, second_user, value)
        card = await db_session.get(Card, grade.card_id)
        card.image_url = f"https://img.example/{value}.png"
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/social/explore", headers=_headers(created_user))
    )
    assert len(data["cards"]) == 2
    assert all(c["image_url"] for c in data["cards"]), "a tile IS its art"
    assert data["cards"][0]["username"] == "sharer"
    # The first tile leads a hero band; the client's grid math relies on it.
    assert data["cards"][0]["is_hero"] is True


@pytest.mark.asyncio
async def test_explore_excludes_private_collections_and_your_own_cards(
    client, db_session, created_user, second_user
):
    """A browse grid of your own cards is a mirror, and a private vault
    must not be browsable by strangers."""
    await _claim(client, created_user, "mine")
    await _claim(client, second_user, "hidden", is_private=True)
    for user in (created_user, second_user):
        grade = await _add_graded_card(db_session, user, "100.00")
        card = await db_session.get(Card, grade.card_id)
        card.image_url = "https://img.example/x.png"
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/social/explore", headers=_headers(created_user))
    )
    assert data["cards"] == []


@pytest.mark.asyncio
async def test_search_finds_a_collector_by_exact_email(
    client, created_user, second_user
):
    await _claim(client, created_user, "seeker")
    await _claim(client, second_user, "findme")

    data = assert_envelope_ok(
        await client.get(
            f"/v1/social/search?q={quote(second_user.email)}",
            headers=_headers(created_user),
        )
    )
    assert [u["username"] for u in data] == ["findme"]
    # The address is a lookup key, never something the API hands back.
    assert all("email" not in u for u in data)


@pytest.mark.asyncio
async def test_search_will_not_harvest_emails_by_prefix(
    client, created_user, second_user
):
    """Exact-match only. Partial email search would turn the collector
    directory into an address-harvesting tool."""
    await _claim(client, created_user, "seeker")
    await _claim(client, second_user, "findme")

    local_part = second_user.email.split("@")[0][:6]
    data = assert_envelope_ok(
        await client.get(
            f"/v1/social/search?q={quote(local_part)}",
            headers=_headers(created_user),
        )
    )
    assert [u["username"] for u in data] == []


@pytest.mark.asyncio
async def test_search_finds_a_collector_by_account_id(
    client, created_user, second_user
):
    """The id a support conversation would quote."""
    await _claim(client, created_user, "seeker")
    await _claim(client, second_user, "findme")

    data = assert_envelope_ok(
        await client.get(
            f"/v1/social/search?q={second_user.id}", headers=_headers(created_user)
        )
    )
    assert [u["username"] for u in data] == ["findme"]
