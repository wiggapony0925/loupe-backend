"""Session lifecycle endpoints: dev-login, cookie sign-out, and the 2FA
status/disable pair.

These are the small endpoints that surround the big flows in
``test_auth_flow`` / ``test_mfa_and_lockout``: the environment-gated back door
(``/auth/dev-login``), the browser's only way to drop an HttpOnly cookie
(``/auth/logout``), and the two calls the Settings screen makes to show and
turn off two-factor.
"""

from __future__ import annotations

import uuid

import pyotp
import pytest

from app.auth.dependencies import AUTH_COOKIE
from app.config import get_settings
from app.services import email_service
from tests.conftest import assert_envelope_error, assert_envelope_ok


@pytest.fixture
def no_rate_limit(monkeypatch):
    """Disable the per-IP sliding window so a test that logs in repeatedly
    isn't handed a 429 that has nothing to do with what it asserts."""
    monkeypatch.setattr(
        "app.platform.rate_limit._SlidingWindow.hit",
        lambda self, key: True,
    )


@pytest.fixture
def mfa_disabled_email(monkeypatch):
    """Record every "your two-factor was turned off" security notice."""

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list = []

        async def __call__(self, *args, **kwargs) -> bool:
            self.calls.append((args, kwargs))
            return True

    recorder = _Recorder()
    monkeypatch.setattr(email_service, "send_mfa_disabled", recorder)
    return recorder


async def _enrol_mfa(client, headers) -> tuple[pyotp.TOTP, list[str]]:
    """Take an already-signed-in account all the way through 2FA enrollment."""
    setup = assert_envelope_ok(await client.post("/v1/auth/mfa/setup", headers=headers))
    totp = pyotp.TOTP(setup["secret"])
    enable = assert_envelope_ok(
        await client.post(
            "/v1/auth/mfa/enable", headers=headers, json={"code": totp.now()}
        )
    )
    return totp, enable["backup_codes"]


# ── POST /v1/auth/dev-login ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dev_login_creates_an_account_on_first_use(client):
    """The back door is find-or-CREATE: a fixture email nobody registered still
    yields a working session, which is the whole point for local/e2e work."""
    email = f"dev+{uuid.uuid4().hex[:8]}@example.com"

    body = assert_envelope_ok(
        await client.post("/v1/auth/dev-login", json={"email": email})
    )

    assert body["user"]["email"] == email
    assert body["token_type"] == "bearer"
    me = await client.get(
        "/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200


@pytest.mark.asyncio
async def test_dev_login_twice_returns_the_same_account(client):
    """Find-or-create, not create-always — otherwise every dev-login would fork
    a new account and the seeded data from the last run would be orphaned."""
    email = f"dev+{uuid.uuid4().hex[:8]}@example.com"

    first = assert_envelope_ok(
        await client.post("/v1/auth/dev-login", json={"email": email})
    )
    second = assert_envelope_ok(
        await client.post("/v1/auth/dev-login", json={"email": email})
    )

    assert first["user"]["id"] == second["user"]["id"]


@pytest.mark.asyncio
async def test_dev_login_signs_in_an_existing_password_account_without_the_password(
    client,
):
    """The back door matches on email alone, so where it exists at all it hands
    out a session for a real account that was registered with a password. That
    is exactly why it may only exist where the accounts are disposable —
    see the environment gate below."""
    email = f"pw+{uuid.uuid4().hex[:8]}@example.com"
    await client.post(
        "/v1/auth/register", json={"email": email, "password": "Password123"}
    )

    body = assert_envelope_ok(
        await client.post("/v1/auth/dev-login", json={"email": email})
    )
    assert body["user"]["email"] == email


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ["production", "staging"])
async def test_dev_login_exists_only_in_development_and_test(client, monkeypatch, env):
    """The gate is an allow-list of development/test, not a deny-list of
    production. Staging serves real accounts over the public internet, so a
    passwordless find-or-create with no allowlist and no rate limit is account
    takeover there for anyone who knows an email address.

    404 rather than 403, so the route doesn't even confirm a passwordless
    sign-in path exists.
    """
    gated = get_settings().model_copy(update={"app_env": env})
    monkeypatch.setattr("app.routers.auth.auth.get_settings", lambda: gated)

    resp = await client.post(
        "/v1/auth/dev-login", json={"email": "someone@example.com"}
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ["development", "test"])
async def test_dev_login_still_works_where_it_is_meant_to(client, monkeypatch, env):
    """The counterpart to the gate: local and CI depend on this route, so the
    allow-list must not have closed it everywhere."""
    allowed = get_settings().model_copy(update={"app_env": env})
    monkeypatch.setattr("app.routers.auth.auth.get_settings", lambda: allowed)

    body = assert_envelope_ok(
        await client.post(
            "/v1/auth/dev-login",
            json={"email": f"gate+{uuid.uuid4().hex[:8]}@example.com"},
        )
    )
    assert body["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_dev_login_rejects_a_malformed_email(client):
    resp = await client.post("/v1/auth/dev-login", json={"email": "not-an-email"})
    assert_envelope_error(resp, expected_status=422)


# ── POST /v1/auth/logout ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_logout_ends_a_cookie_session(client):
    """The web signs in on an HttpOnly cookie that JS cannot clear, so sign-out
    has to be a server call: after it, the same browser is anonymous again."""
    email = f"cookie+{uuid.uuid4().hex[:8]}@example.com"
    await client.post("/v1/auth/dev-login", json={"email": email})
    assert client.cookies.get(AUTH_COOKIE)

    # No Authorization header — the cookie alone is authenticating here.
    assert (await client.get("/v1/me")).status_code == 200

    resp = await client.post("/v1/auth/logout")
    assert resp.status_code == 204

    assert (await client.get("/v1/me")).status_code == 401


@pytest.mark.asyncio
async def test_logout_works_without_a_session(client):
    """Sign-out must never 401. A user whose token already expired still needs
    the stale cookie cleared, and a client retrying sign-out shouldn't error."""
    resp = await client.post("/v1/auth/logout")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_logout_leaves_other_devices_signed_in(
    client, created_user, auth_headers
):
    """Clearing one browser's cookie is not a revocation — that's ``/logout-all``.
    A bearer token issued to the phone must survive the web signing out."""
    resp = await client.post("/v1/auth/logout")
    assert resp.status_code == 204

    me = await client.get("/v1/me", headers=auth_headers)
    assert me.status_code == 200


# ── GET /v1/auth/mfa/status ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mfa_status_is_false_before_enrollment(client, auth_headers):
    body = assert_envelope_ok(
        await client.get("/v1/auth/mfa/status", headers=auth_headers)
    )
    assert body["enabled"] is False


@pytest.mark.asyncio
async def test_mfa_status_requires_a_session(client):
    """Whether an account has 2FA on is information about that account — it is
    only ever answered for the caller's own session."""
    resp = await client.get("/v1/auth/mfa/status")
    assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
async def test_mfa_status_flips_after_enrollment(client, no_rate_limit):
    reg = assert_envelope_ok(
        await client.post(
            "/v1/auth/register",
            json={
                "email": f"st+{uuid.uuid4().hex[:8]}@example.com",
                "password": "Password123",
            },
        ),
        expected_status=201,
    )
    headers = {"Authorization": f"Bearer {reg['access_token']}"}
    await _enrol_mfa(client, headers)

    body = assert_envelope_ok(await client.get("/v1/auth/mfa/status", headers=headers))
    assert body["enabled"] is True


# ── POST /v1/auth/mfa/disable ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabling_mfa_requires_a_current_code(client, no_rate_limit):
    """Turning the second factor OFF is exactly what someone holding a stolen
    session wants to do, so it re-verifies the factor first."""
    reg = assert_envelope_ok(
        await client.post(
            "/v1/auth/register",
            json={
                "email": f"dis+{uuid.uuid4().hex[:8]}@example.com",
                "password": "Password123",
            },
        ),
        expected_status=201,
    )
    headers = {"Authorization": f"Bearer {reg['access_token']}"}
    totp, _codes = await _enrol_mfa(client, headers)

    bad = await client.post(
        "/v1/auth/mfa/disable", headers=headers, json={"code": "000000"}
    )
    assert_envelope_error(bad, expected_status=400)
    assert (
        assert_envelope_ok(await client.get("/v1/auth/mfa/status", headers=headers))[
            "enabled"
        ]
        is True
    )

    ok = await client.post(
        "/v1/auth/mfa/disable", headers=headers, json={"code": totp.now()}
    )
    assert ok.status_code == 204
    assert (
        assert_envelope_ok(await client.get("/v1/auth/mfa/status", headers=headers))[
            "enabled"
        ]
        is False
    )


@pytest.mark.asyncio
async def test_a_backup_code_can_also_disable_mfa(client, no_rate_limit):
    """Backup codes exist for the case where the authenticator is gone — which
    is precisely when someone needs to turn 2FA off."""
    reg = assert_envelope_ok(
        await client.post(
            "/v1/auth/register",
            json={
                "email": f"bk+{uuid.uuid4().hex[:8]}@example.com",
                "password": "Password123",
            },
        ),
        expected_status=201,
    )
    headers = {"Authorization": f"Bearer {reg['access_token']}"}
    _totp, codes = await _enrol_mfa(client, headers)

    resp = await client.post(
        "/v1/auth/mfa/disable", headers=headers, json={"code": codes[0]}
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_disabling_mfa_that_was_never_on_is_a_no_op(
    client, auth_headers, mfa_disabled_email
):
    """Idempotent by design: a client that double-taps "turn off" gets 204
    rather than an error about a code it was never asked for.

    A no-op must also stay silent. "Your two-factor was turned off" is the
    alert an account owner has to believe instantly, so it may only be sent
    when the factor actually came off; firing it on a call that changed
    nothing is a false alarm that teaches people to ignore it. It is also the
    amplifier: without this, anyone holding a session could loop the (unthrottled)
    endpoint and send unbounded mail from our sending domain.
    """
    for _ in range(6):
        resp = await client.post(
            "/v1/auth/mfa/disable", headers=auth_headers, json={"code": "000000"}
        )
        assert resp.status_code == 204

    assert mfa_disabled_email.calls == []


@pytest.mark.asyncio
async def test_a_real_disable_still_raises_the_security_alert(
    client, no_rate_limit, mfa_disabled_email
):
    """The other half of the rule: when the second factor genuinely comes off,
    the owner is told exactly once — that notice is how someone finds out an
    attacker with their session just removed their 2FA."""
    reg = assert_envelope_ok(
        await client.post(
            "/v1/auth/register",
            json={
                "email": f"alert+{uuid.uuid4().hex[:8]}@example.com",
                "password": "Password123",
            },
        ),
        expected_status=201,
    )
    headers = {"Authorization": f"Bearer {reg['access_token']}"}
    totp, _codes = await _enrol_mfa(client, headers)

    resp = await client.post(
        "/v1/auth/mfa/disable", headers=headers, json={"code": totp.now()}
    )
    assert resp.status_code == 204
    assert len(mfa_disabled_email.calls) == 1

    # Turning off what is already off adds no second alert.
    assert (
        await client.post(
            "/v1/auth/mfa/disable", headers=headers, json={"code": totp.now()}
        )
    ).status_code == 204
    assert len(mfa_disabled_email.calls) == 1


@pytest.mark.asyncio
async def test_mfa_disable_requires_a_session(client):
    resp = await client.post("/v1/auth/mfa/disable", json={"code": "000000"})
    assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
async def test_mfa_disable_rejects_a_body_with_no_code(client, auth_headers):
    resp = await client.post("/v1/auth/mfa/disable", headers=auth_headers, json={})
    assert_envelope_error(resp, expected_status=422)
