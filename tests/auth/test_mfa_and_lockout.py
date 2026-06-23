"""Login brute-force lockout + TOTP MFA sign-in flow."""

import pyotp
import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok


@pytest.fixture
def no_rate_limit(monkeypatch):
    """Disable the per-IP sliding-window limiter so account-level lockout (the
    thing under test) isn't masked by the IP throttle."""
    monkeypatch.setattr(
        "app.platform.rate_limit._SlidingWindow.hit",
        lambda self, key: True,
    )


async def _register(client, email: str, password: str = "Password123"):
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "display_name": "T"},
    )
    return assert_envelope_ok(resp, expected_status=201)


@pytest.mark.asyncio
async def test_account_locks_after_repeated_failures(client, no_rate_limit):
    email = "locktest@example.com"
    await _register(client, email)

    # Five wrong-password attempts are each rejected as bad credentials.
    for _ in range(5):
        resp = await client.post(
            "/v1/auth/login", json={"email": email, "password": "wrong-one"}
        )
        assert_envelope_error(resp, expected_status=401)

    # The account is now locked: even the CORRECT password is refused (429).
    resp = await client.post(
        "/v1/auth/login", json={"email": email, "password": "Password123"}
    )
    assert_envelope_error(resp, expected_status=429)


@pytest.mark.asyncio
async def test_successful_login_resets_failure_counter(client, no_rate_limit):
    email = "resettest@example.com"
    await _register(client, email)

    # Four failures (one below the threshold), then a success clears the count.
    for _ in range(4):
        await client.post("/v1/auth/login", json={"email": email, "password": "nope"})
    ok = await client.post(
        "/v1/auth/login", json={"email": email, "password": "Password123"}
    )
    body = assert_envelope_ok(ok)
    assert body["access_token"]

    # A fresh run of four more failures still doesn't lock (counter was reset).
    for _ in range(4):
        resp = await client.post(
            "/v1/auth/login", json={"email": email, "password": "nope"}
        )
        assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
async def test_mfa_enrollment_and_login_challenge(client, no_rate_limit):
    email = "mfatest@example.com"
    reg = await _register(client, email)
    headers = {"Authorization": f"Bearer {reg['access_token']}"}

    # Enroll: setup → confirm with a live TOTP code → backup codes returned.
    setup = assert_envelope_ok(await client.post("/v1/auth/mfa/setup", headers=headers))
    secret = setup["secret"]
    assert setup["qr_svg"].startswith("data:image/svg+xml")

    totp = pyotp.TOTP(secret)
    enable = assert_envelope_ok(
        await client.post(
            "/v1/auth/mfa/enable", headers=headers, json={"code": totp.now()}
        )
    )
    backup_codes = enable["backup_codes"]
    assert len(backup_codes) == 10

    # Now login returns a challenge, not tokens.
    challenge = assert_envelope_ok(
        await client.post(
            "/v1/auth/login", json={"email": email, "password": "Password123"}
        )
    )
    assert challenge["mfa_required"] is True
    assert challenge["mfa_token"]
    assert challenge["access_token"] is None

    # A wrong code is rejected.
    bad = await client.post(
        "/v1/auth/mfa/verify",
        json={"mfa_token": challenge["mfa_token"], "code": "000000"},
    )
    assert_envelope_error(bad, expected_status=401)

    # The right TOTP code completes sign-in.
    verified = assert_envelope_ok(
        await client.post(
            "/v1/auth/mfa/verify",
            json={"mfa_token": challenge["mfa_token"], "code": totp.now()},
        )
    )
    assert verified["access_token"]
    assert verified["user"]["email"] == email


@pytest.mark.asyncio
async def test_mfa_backup_code_consumed_once(client, no_rate_limit):
    email = "backuptest@example.com"
    reg = await _register(client, email)
    headers = {"Authorization": f"Bearer {reg['access_token']}"}

    setup = assert_envelope_ok(await client.post("/v1/auth/mfa/setup", headers=headers))
    totp = pyotp.TOTP(setup["secret"])
    enable = assert_envelope_ok(
        await client.post(
            "/v1/auth/mfa/enable", headers=headers, json={"code": totp.now()}
        )
    )
    code = enable["backup_codes"][0]

    async def login_challenge():
        body = assert_envelope_ok(
            await client.post(
                "/v1/auth/login",
                json={"email": email, "password": "Password123"},
            )
        )
        return body["mfa_token"]

    # First use of the backup code works.
    ok = await client.post(
        "/v1/auth/mfa/verify",
        json={"mfa_token": await login_challenge(), "code": code},
    )
    assert assert_envelope_ok(ok)["access_token"]

    # Re-using the same backup code is now rejected (consumed).
    reused = await client.post(
        "/v1/auth/mfa/verify",
        json={"mfa_token": await login_challenge(), "code": code},
    )
    assert_envelope_error(reused, expected_status=401)
