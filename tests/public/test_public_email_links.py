"""The two signed-link landings: `/v1/public/verify-email` and
`/v1/public/unsubscribe`.

Both are opened straight from an email — by a signed-out human on a strange
device, or by a mail provider's own robot doing RFC 8058 one-click. So the
HMAC token is the *entire* authorization story, and the properties that make
that safe are the ones worth pinning: the token names exactly one account, it
is scoped to one purpose, it can only move a flag in the harmless direction,
and replaying it changes nothing.

``tests/email`` covers the happy paths from the sending side; these cover the
endpoints' edges.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.user import UserSettings
from app.services.auth import email_verify_service, unsubscribe_service
from tests.conftest import assert_envelope_error
from tests.factories import make_user


async def _settings_row(db, user_id) -> UserSettings:
    return (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one()


# ── /v1/public/verify-email ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_verify_link_still_works_a_year_later(client, db_session):
    """These tokens deliberately carry no expiry: someone digs up the welcome
    email months later and the link must still confirm the address. There is no
    "expired" state to test — only valid, tampered, and unknown."""
    user = await make_user(db_session)
    token = email_verify_service.mint_token(str(user.id))

    resp = await client.get("/v1/public/verify-email", params={"token": token})
    assert resp.status_code == 200

    await db_session.refresh(user)
    assert user.email_verified is True


@pytest.mark.asyncio
async def test_replaying_a_verify_link_keeps_the_original_timestamp(client, db_session):
    """Replay is harmless by construction — the link can only flip verification
    ON. It must also be a no-op: re-clicking must not rewrite *when* the
    address was confirmed."""
    user = await make_user(db_session)
    token = email_verify_service.mint_token(str(user.id))

    assert (
        await client.get("/v1/public/verify-email", params={"token": token})
    ).status_code == 200
    await db_session.refresh(user)
    first_seen = user.email_verified_at

    resp = await client.get("/v1/public/verify-email", params={"token": token})
    assert resp.status_code == 200

    await db_session.refresh(user)
    assert user.email_verified_at == first_seen


@pytest.mark.asyncio
async def test_a_verify_signature_from_another_account_is_refused(client, db_session):
    """The signature covers the user id, so lifting one account's ``sig`` onto
    another's id must not verify that other account."""
    victim = await make_user(db_session)
    attacker = await make_user(db_session)
    _, _, sig = email_verify_service.mint_token(str(attacker.id)).partition(".")

    resp = await client.get(
        "/v1/public/verify-email", params={"token": f"{victim.id}.{sig}"}
    )
    assert resp.status_code == 400

    await db_session.refresh(victim)
    assert victim.email_verified is False


@pytest.mark.asyncio
async def test_an_unsubscribe_token_cannot_verify_an_email(client, db_session):
    """The two link types are signed with purpose-bound keys. An unsubscribe
    link handed to the verify endpoint must not confirm the address — otherwise
    a mail provider's automatic one-click fetch would silently verify people."""
    user = await make_user(db_session)
    token = unsubscribe_service.mint_token(str(user.id))

    resp = await client.get("/v1/public/verify-email", params={"token": token})
    assert resp.status_code == 400

    await db_session.refresh(user)
    assert user.email_verified is False


@pytest.mark.asyncio
async def test_a_well_formed_token_for_a_stranger_is_refused(client):
    """Correctly signed, but the account is gone (or never existed). The
    endpoint answers the same 400 page rather than confirming the id exists."""
    token = email_verify_service.mint_token(str(uuid.uuid4()))

    resp = await client.get("/v1/public/verify-email", params={"token": token})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_a_deleted_account_cannot_be_verified(client, db_session):
    """Soft-deleted means gone. A stale link in an old inbox must not write to
    a closed account."""
    user = await make_user(db_session)
    user.deleted_at = datetime.now(UTC)
    await db_session.commit()
    token = email_verify_service.mint_token(str(user.id))

    resp = await client.get("/v1/public/verify-email", params={"token": token})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_the_verify_failure_page_is_human_readable(client):
    """This is rendered in a browser by someone who just clicked a link — the
    failure has to be a page that tells them what to do, not a JSON error."""
    resp = await client.get(
        "/v1/public/verify-email", params={"token": "garbage-garbage"}
    )
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/html")
    assert "didn't work" in resp.text


@pytest.mark.asyncio
async def test_a_truncated_token_is_rejected_before_any_lookup(client):
    resp = await client.get("/v1/public/verify-email", params={"token": "short"})
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_the_verify_success_page_is_branded_html(client, db_session):
    """The success case lands in a browser too, so it renders a page with a way
    back into the app — not a bare 200 or a JSON envelope."""
    user = await make_user(db_session)
    token = email_verify_service.mint_token(str(user.id))

    resp = await client.get("/v1/public/verify-email", params={"token": token})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "LOUPE" in resp.text
    assert "/app" in resp.text


# ── /v1/public/unsubscribe ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_click_post_unsubscribes_without_a_session(client, db_session):
    """Mail providers POST this themselves for RFC 8058 one-click — no browser,
    no cookies, no human. It has to work on the POST alone."""
    user = await make_user(db_session)
    token = unsubscribe_service.mint_token(str(user.id))

    resp = await client.post("/v1/public/unsubscribe", params={"token": token})
    assert resp.status_code == 200

    assert (
        await _settings_row(db_session, user.id)
    ).email_announcements_enabled is False


@pytest.mark.asyncio
async def test_replaying_an_unsubscribe_link_stays_unsubscribed(client, db_session):
    """The token can only ever turn announcement email OFF, so a replay — a
    provider retry, a forwarded email, a prefetching mail client — must be a
    no-op rather than a toggle back on."""
    user = await make_user(db_session)
    token = unsubscribe_service.mint_token(str(user.id))

    for _ in range(3):
        resp = await client.get("/v1/public/unsubscribe", params={"token": token})
        assert resp.status_code == 200

    assert (
        await _settings_row(db_session, user.id)
    ).email_announcements_enabled is False


@pytest.mark.asyncio
async def test_a_verify_token_cannot_unsubscribe_someone(client, db_session):
    """Purpose-bound keys again, in the other direction: clicking a verify link
    must not silently opt the reader out of product email."""
    user = await make_user(db_session)
    token = email_verify_service.mint_token(str(user.id))

    resp = await client.get("/v1/public/unsubscribe", params={"token": token})
    assert resp.status_code == 400

    assert (
        await _settings_row(db_session, user.id)
    ).email_announcements_enabled is True


@pytest.mark.asyncio
async def test_another_accounts_signature_cannot_unsubscribe_me(client, db_session):
    victim = await make_user(db_session)
    attacker = await make_user(db_session)
    _, _, sig = unsubscribe_service.mint_token(str(attacker.id)).partition(".")

    resp = await client.get(
        "/v1/public/unsubscribe", params={"token": f"{victim.id}.{sig}"}
    )
    assert resp.status_code == 400

    assert (
        await _settings_row(db_session, victim.id)
    ).email_announcements_enabled is True


@pytest.mark.asyncio
async def test_a_well_formed_unsubscribe_token_for_a_stranger_is_refused(client):
    token = unsubscribe_service.mint_token(str(uuid.uuid4()))

    resp = await client.get("/v1/public/unsubscribe", params={"token": token})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_a_deleted_account_cannot_be_unsubscribed(client, db_session):
    """Soft-deleted means gone, and the two signed links have to agree on
    that — ``verify-email`` already refuses a closed account. This endpoint is
    unauthenticated, so the same rule has to hold here: no writes to a closed
    account, and above all no CREATING a preferences row for one."""
    user = await make_user(db_session)
    user.deleted_at = datetime.now(UTC)
    await db_session.commit()
    token = unsubscribe_service.mint_token(str(user.id))

    resp = await client.get("/v1/public/unsubscribe", params={"token": token})
    assert resp.status_code == 400

    # The existing row is untouched — the opt-out was never recorded.
    assert (
        await _settings_row(db_session, user.id)
    ).email_announcements_enabled is True


@pytest.mark.asyncio
async def test_a_deleted_account_gets_no_preferences_row_from_a_stale_link(
    client, db_session
):
    """The sharper half of the same rule. ``apply_unsubscribe`` creates the
    settings row when an older account has none — which meant a stale link in
    a stranger's inbox could conjure fresh rows for an account that had already
    been closed."""
    from app.models.user import User

    user = User(
        email=f"gone+{uuid.uuid4().hex[:8]}@example.com",
        deleted_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    token = unsubscribe_service.mint_token(str(user.id))

    resp = await client.get("/v1/public/unsubscribe", params={"token": token})
    assert resp.status_code == 400

    rows = (
        (
            await db_session.execute(
                select(UserSettings).where(UserSettings.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_unsubscribing_creates_the_preferences_row_if_it_is_missing(
    client, db_session
):
    """Older accounts predate the settings row. The opt-out has to be recorded
    anyway — "no row" must not read as "never asked to stop"."""
    from app.models.user import User

    user = User(email=f"bare+{uuid.uuid4().hex[:8]}@example.com")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    token = unsubscribe_service.mint_token(str(user.id))

    resp = await client.get("/v1/public/unsubscribe", params={"token": token})
    assert resp.status_code == 200

    assert (
        await _settings_row(db_session, user.id)
    ).email_announcements_enabled is False


@pytest.mark.asyncio
async def test_the_unsubscribe_failure_page_is_human_readable(client):
    resp = await client.post(
        "/v1/public/unsubscribe", params={"token": "garbage-garbage"}
    )
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/html")
    assert "didn't work" in resp.text


@pytest.mark.asyncio
async def test_a_truncated_unsubscribe_token_is_rejected(client):
    resp = await client.get("/v1/public/unsubscribe", params={"token": "short"})
    assert_envelope_error(resp, expected_status=422)
