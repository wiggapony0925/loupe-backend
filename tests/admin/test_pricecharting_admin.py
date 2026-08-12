"""Router tests for `/v1/admin/pricecharting` — the tier & fallback page.

The app adapts to whatever PriceCharting plan is live by *probing* the account
rather than reading a config flag, so this page is how an operator sees which
tier the app believes it is on. The rules that matter here: the read is cheap
(cached — opening the portal must not probe a metered account), the probe
button is the only thing that forces a fresh look, and a sync on a plan
without the bulk CSV is a no-op with a reason rather than an error.

Every test stubs the upstream probe: a real one would hit PriceCharting with
the developer's own token.
"""

from __future__ import annotations

import contextlib

import pytest
import pytest_asyncio

from app.auth.jwt import issue_token
from app.config import get_settings
from app.integrations.pricecharting import csv_sync, tiers
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


@pytest_asyncio.fixture
async def admin_user(db_session):
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    token, _ = issue_token(admin_user.id, "access")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _no_upstream(monkeypatch):
    """Default every test to "no token": ``detect`` then short-circuits before
    any HTTP call. Tests that want a tier re-stub the probes themselves."""
    monkeypatch.setattr(tiers, "token", lambda: None)


@contextlib.contextmanager
def _csv_url(value: str | None):
    settings = get_settings()
    prev = settings.pricecharting_csv_url
    settings.pricecharting_csv_url = value  # type: ignore[misc]
    try:
        yield
    finally:
        settings.pricecharting_csv_url = prev  # type: ignore[misc]


# ── Authorization boundary ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_and_sync_are_not_public(client):
    """Both are actions against a paid upstream account — an anonymous caller
    could burn the monthly quota or kick off a full mirror rebuild."""
    assert_envelope_error(
        await client.post("/v1/admin/pricecharting/probe"), expected_status=401
    )
    assert_envelope_error(
        await client.post("/v1/admin/pricecharting/sync"), expected_status=401
    )


@pytest.mark.asyncio
async def test_probe_and_sync_are_closed_to_ordinary_users(client, auth_headers):
    """Being signed in does not make a collector an operator."""
    assert_envelope_error(
        await client.post("/v1/admin/pricecharting/probe", headers=auth_headers),
        expected_status=403,
    )
    assert_envelope_error(
        await client.post("/v1/admin/pricecharting/sync", headers=auth_headers),
        expected_status=403,
    )


# ── Overview ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_without_a_token_reports_the_modelled_fallback(
    client, admin_headers
):
    """No subscription is a supported state, not a broken one: prices still
    resolve from the catalog with a modelled ladder. The page has to say that
    plainly, because "nothing configured" is what a new deployment looks like."""
    data = assert_envelope_ok(
        await client.get("/v1/admin/pricecharting", headers=admin_headers)
    )
    assert data["configured"] is False
    assert data["tier"]["key"] == "none"
    assert data["strategy"]["key"] == "modeled"
    assert data["mirror"] == {"ready": False, "rows": 0, "synced_at": None}


@pytest.mark.asyncio
async def test_overview_always_shows_the_whole_chain_with_one_active_rung(
    client, admin_headers
):
    """The page's job is to explain *why* pricing behaves as it does, so it
    renders every tier best → worst and marks the one in force. Exactly one
    rung may be active — two would mean the tier derivation is ambiguous."""
    data = assert_envelope_ok(
        await client.get("/v1/admin/pricecharting", headers=admin_headers)
    )
    assert [rung["tier"] for rung in data["fallback_chain"]] == [
        "legendary",
        "premium",
        "collector",
        "none",
    ]
    active = [rung for rung in data["fallback_chain"] if rung["active"]]
    assert len(active) == 1
    assert active[0]["tier"] == data["tier"]["key"]


@pytest.mark.asyncio
async def test_opening_the_page_reuses_the_cached_probe(
    client, admin_headers, monkeypatch
):
    """Capabilities change only when someone changes the plan, so the read path
    is cached. If merely loading the portal re-probed, every page view would
    spend a request against a metered account."""
    probes: list[str] = []

    async def _api():
        probes.append("api")
        return True, True, "graded"

    async def _csv():
        return False, "no csv"

    monkeypatch.setattr(tiers, "token", lambda: "tok")
    monkeypatch.setattr(tiers, "_probe_api", _api)
    monkeypatch.setattr(tiers, "_probe_csv", _csv)

    first = assert_envelope_ok(
        await client.get("/v1/admin/pricecharting", headers=admin_headers)
    )
    assert probes == ["api"]  # cold cache — one probe
    assert first["tier"]["key"] == "premium"

    second = assert_envelope_ok(
        await client.get("/v1/admin/pricecharting", headers=admin_headers)
    )
    assert probes == ["api"], "a second page load must not re-probe"
    assert second["tier"]["key"] == "premium"


@pytest.mark.asyncio
async def test_the_probe_button_adopts_a_changed_subscription_immediately(
    client, admin_headers, monkeypatch
):
    """This is the whole reason the endpoint exists: after upgrading the plan
    an operator presses "re-probe" and the app follows the new tier without a
    redeploy — even though the cache still holds the old answer."""
    capability = {"graded": False}

    async def _api():
        return True, capability["graded"], "note"

    async def _csv():
        return False, "no csv"

    monkeypatch.setattr(tiers, "token", lambda: "tok")
    monkeypatch.setattr(tiers, "_probe_api", _api)
    monkeypatch.setattr(tiers, "_probe_csv", _csv)

    before = assert_envelope_ok(
        await client.get("/v1/admin/pricecharting", headers=admin_headers)
    )
    assert before["tier"]["key"] == "collector"

    capability["graded"] = True  # the subscription is upgraded upstream
    stale = assert_envelope_ok(
        await client.get("/v1/admin/pricecharting", headers=admin_headers)
    )
    assert stale["tier"]["key"] == "collector", "the cache is still authoritative"

    forced = assert_envelope_ok(
        await client.post("/v1/admin/pricecharting/probe", headers=admin_headers)
    )
    assert forced["tier"]["key"] == "premium"
    assert forced["capabilities"]["graded_fields"] is True


# ── Mirror sync ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_without_a_csv_url_is_a_reasoned_no_op(client, admin_headers):
    """The bulk CSV is Legendary-only and its URL is account-specific. Pressing
    sync on a lower plan is a normal thing to do — it must answer "here's why
    nothing happened" rather than fail."""
    with _csv_url(None):
        data = assert_envelope_ok(
            await client.post("/v1/admin/pricecharting/sync", headers=admin_headers)
        )
    assert data == {"ok": False, "rows": 0, "reason": "no_csv_url"}


@pytest.mark.asyncio
async def test_sync_replaces_the_local_mirror_and_reports_the_row_count(
    client, admin_headers, monkeypatch
):
    """A sync is a full snapshot swap, and the row count is how an operator
    confirms it landed — the same number then shows up as the mirror status."""
    csv_text = (
        "id,product-name,console-name,loose-price,manual-only-price,sales-volume\n"
        "6910,Charizard #4,Pokemon Base Set,30000,250000,1234\n"
        "58,Pikachu #58,Pokemon Base Set,500,,7\n"
        ",,,100,,\n"  # no id/name — not a product, must be dropped
    )

    async def _download(url: str) -> str:
        assert url == "https://example.test/guide.csv"
        return csv_text

    monkeypatch.setattr(csv_sync, "_download_csv", _download)
    csv_sync.reset_ready_cache()

    with _csv_url("https://example.test/guide.csv"):
        data = assert_envelope_ok(
            await client.post("/v1/admin/pricecharting/sync", headers=admin_headers)
        )
        assert data == {"ok": True, "rows": 2}

        overview = assert_envelope_ok(
            await client.get("/v1/admin/pricecharting", headers=admin_headers)
        )
    assert overview["mirror"]["ready"] is True
    assert overview["mirror"]["rows"] == 2
    assert overview["mirror"]["synced_at"] is not None


@pytest.mark.asyncio
async def test_a_failed_download_is_reported_not_raised(
    client, admin_headers, monkeypatch
):
    """An expired CSV link is an operator problem with an obvious fix. The
    portal should show the reason instead of a 500 with no explanation."""

    async def _boom(url: str) -> str:
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(csv_sync, "_download_csv", _boom)

    with _csv_url("https://example.test/expired.csv"):
        data = assert_envelope_ok(
            await client.post("/v1/admin/pricecharting/sync", headers=admin_headers)
        )
    assert data["ok"] is False
    assert data["rows"] == 0
    assert "403 Forbidden" in data["reason"]
