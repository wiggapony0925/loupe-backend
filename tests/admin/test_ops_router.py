"""Router + security tests for the admin Operations endpoints.

Verifies admin gating, that the database explorer exposes structure only (never
row data), and that the cloud panel degrades gracefully without GCP config.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.models.audit import AuditLog
from app.schemas.ops import CloudLogEntry
from tests.conftest import assert_envelope_error, assert_envelope_ok


@contextlib.contextmanager
def _as_admin(email: str):
    """Temporarily add an email to the admin allowlist (tests start empty)."""
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = email  # type: ignore[misc]
    try:
        yield
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


@pytest.mark.asyncio
async def test_ops_endpoints_require_admin(client):
    for path in ("/v1/admin/health", "/v1/admin/database/tables", "/v1/admin/audit"):
        resp = await client.get(path)
        assert resp.status_code in (401, 403), path


@pytest.mark.asyncio
async def test_health_endpoint_admin(client, created_user, auth_headers):
    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/health", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] in {"ok", "warn", "down"}
    assert any(c["key"] == "database" for c in body["checks"])


@pytest.mark.asyncio
async def test_database_explorer_is_metadata_only(client, created_user, auth_headers):
    with _as_admin(created_user.email):
        tables = await client.get("/v1/admin/database/tables", headers=auth_headers)
        detail = await client.get(
            "/v1/admin/database/tables/users", headers=auth_headers
        )
        missing = await client.get(
            "/v1/admin/database/tables/nope", headers=auth_headers
        )

    assert tables.status_code == 200
    overview = tables.json()["data"]
    assert overview["table_count"] > 0
    # Structure only — never a row-data field.
    sample = overview["tables"][0]
    assert set(sample) == {"name", "columns", "row_estimate", "foreign_keys"}

    assert detail.status_code == 200
    cols = detail.json()["data"]["columns"]
    # Columns carry shape, not values.
    assert all(
        set(c) == {"name", "type", "nullable", "primary_key", "foreign_key"}
        for c in cols
    )

    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_cloud_unconfigured_is_graceful(
    client, created_user, auth_headers, monkeypatch
):
    # Force "no project resolvable" so the test is deterministic even on a
    # machine with ambient Application Default Credentials.
    monkeypatch.setattr(
        "app.services.admin.gcp_service._project_id", lambda _settings: None
    )
    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/cloud", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()["data"]
    # No GCP project → clean "not configured", no error.
    assert body["configured"] is False
    assert body["detail"]
    assert body["services"] == []


# ── The rest of the Operations surface ──────────────────────────────────────
# audit facets, cloud logs, the schema graph, the environment manager, and the
# integrations catalog. Everything here is read-only observability, so the two
# rules that matter are: only admins may look, and looking must never reveal a
# credential.

_OPS_READ_PATHS = (
    "/v1/admin/audit/facets",
    "/v1/admin/cloud/logs",
    "/v1/admin/database/graph",
    "/v1/admin/env",
    "/v1/admin/integrations",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _OPS_READ_PATHS)
async def test_ops_read_surface_rejects_anonymous_callers(client, path):
    """No token at all is a 401, not a 403 — the caller has no identity yet,
    so the client knows to authenticate rather than to give up."""
    assert_envelope_error(await client.get(path), expected_status=401)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _OPS_READ_PATHS)
async def test_ops_read_surface_rejects_ordinary_users(client, auth_headers, path):
    """A signed-in but non-admin account is a 403. These pages expose the
    shape of production — schema, deploy logs, which providers are wired up —
    so ordinary accounts must not reach them even by URL guessing."""
    assert_envelope_error(
        await client.get(path, headers=auth_headers), expected_status=403
    )


# ── GET /v1/admin/audit/facets ──
@pytest.mark.asyncio
async def test_audit_facets_lists_each_action_and_table_once(
    client, created_user, auth_headers, db_session
):
    """The facets feed the viewer's filter dropdowns, so repeated actions must
    collapse to one entry — otherwise a busy trail renders a dropdown with the
    same value a thousand times."""
    db_session.add_all(
        [
            AuditLog(user_id=created_user.id, action="job.create", target_table="jobs"),
            AuditLog(user_id=created_user.id, action="job.create", target_table="jobs"),
            AuditLog(user_id=created_user.id, action="user.ban", target_table="users"),
            # An action with no target table at all — legal in the model.
            AuditLog(user_id=created_user.id, action="admin.login"),
        ]
    )
    await db_session.commit()

    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/audit/facets", headers=auth_headers)

    body = assert_envelope_ok(resp)
    assert body["actions"] == ["admin.login", "job.create", "user.ban"]
    # NULL target tables are dropped rather than surfacing an empty option.
    assert body["tables"] == ["jobs", "users"]


@pytest.mark.asyncio
async def test_audit_facets_on_an_empty_trail_is_not_an_error(
    client, created_user, auth_headers
):
    """A fresh deployment has no audit rows; the dropdowns should come back
    empty rather than 404/500 and blank the whole viewer page."""
    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/audit/facets", headers=auth_headers)

    body = assert_envelope_ok(resp)
    assert body == {"actions": [], "tables": []}


# ── GET /v1/admin/cloud/logs ──
@pytest.mark.asyncio
async def test_cloud_logs_returns_the_tail_with_the_requested_limit(
    client, created_user, auth_headers, monkeypatch
):
    """The log tail is a live Cloud Logging call, stubbed here: the router's
    job is to hand the requested limit through and serialise what comes back."""
    seen: dict[str, object] = {}

    def _fake_logs(project: str, limit: int) -> list[CloudLogEntry]:
        seen["project"] = project
        seen["limit"] = limit
        return [
            CloudLogEntry(
                timestamp=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
                severity="ERROR",
                service="loupe-api",
                message="boom",
            )
        ]

    monkeypatch.setattr(
        "app.services.admin.gcp_service._project_id", lambda _settings: "loupe-prod"
    )
    monkeypatch.setattr("app.services.admin.gcp_service._load_logs_sync", _fake_logs)

    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/cloud/logs?limit=5", headers=auth_headers)

    body = assert_envelope_ok(resp)
    assert seen == {"project": "loupe-prod", "limit": 5}
    assert body[0]["severity"] == "ERROR"
    assert body[0]["service"] == "loupe-api"
    assert body[0]["message"] == "boom"


@pytest.mark.asyncio
async def test_cloud_logs_without_a_project_is_empty_not_an_error(
    client, created_user, auth_headers, monkeypatch
):
    """Local and CI environments have no GCP project. The panel renders an
    empty tail instead of erroring, so the portal stays usable off-cloud."""
    monkeypatch.setattr(
        "app.services.admin.gcp_service._project_id", lambda _settings: None
    )
    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/cloud/logs", headers=auth_headers)

    assert assert_envelope_ok(resp) == []


@pytest.mark.asyncio
async def test_cloud_logs_swallows_a_logging_outage(
    client, created_user, auth_headers, monkeypatch
):
    """A Cloud Logging failure (revoked viewer role, API outage) must not 500
    the admin portal — losing the log tail is not losing the page."""

    def _boom(project: str, limit: int) -> list[CloudLogEntry]:
        raise RuntimeError("permission denied on logging.logEntries.list")

    monkeypatch.setattr(
        "app.services.admin.gcp_service._project_id", lambda _settings: "loupe-prod"
    )
    monkeypatch.setattr("app.services.admin.gcp_service._load_logs_sync", _boom)

    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/cloud/logs", headers=auth_headers)

    assert assert_envelope_ok(resp) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101])
async def test_cloud_logs_limit_is_bounded(client, created_user, auth_headers, limit):
    """Each entry carries up to 1 KB of message, so an unbounded limit is a
    memory and latency hazard — the query is capped at 1..100."""
    with _as_admin(created_user.email):
        resp = await client.get(
            f"/v1/admin/cloud/logs?limit={limit}", headers=auth_headers
        )
    assert_envelope_error(resp, expected_status=422)


# ── GET /v1/admin/database/graph ──
@pytest.mark.asyncio
async def test_database_graph_is_foreign_key_topology_only(
    client, created_user, auth_headers
):
    """The graph draws the schema, never its contents: nodes carry a table name
    and a column count, edges carry the FK column — no row data, no values."""
    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/database/graph", headers=auth_headers)

    body = assert_envelope_ok(resp)
    assert body["nodes"], "expected the mapped tables as nodes"
    assert all(set(n) == {"table", "columns"} for n in body["nodes"])
    assert all(set(e) == {"source", "target", "label"} for e in body["edges"])
    # audit_log.user_id → users is a known edge of the real schema.
    assert any(
        e["source"] == "audit_log"
        and e["target"] == "users"
        and "user_id" in e["label"]
        for e in body["edges"]
    )


# ── GET /v1/admin/env ──
@pytest.mark.asyncio
async def test_env_reports_the_running_environment_and_its_groups(
    client, created_user, auth_headers
):
    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/env", headers=auth_headers)

    body = assert_envelope_ok(resp)
    assert body["app_env"] == get_settings().app_env
    by_key = {v["key"]: v for v in body["variables"]}
    assert {"DATABASE_URL", "STRIPE_SECRET_KEY", "GCP_REGION"} <= set(by_key)
    for var in body["variables"]:
        assert var["key"] and var["label"] and var["group"] and var["description"]


@pytest.mark.asyncio
async def test_env_never_puts_a_secret_value_on_the_wire(
    client, created_user, auth_headers, monkeypatch
):
    """The cardinal rule of this endpoint. An admin needs to confirm a key is
    *present*, never to read it — so a secret reports only presence and length,
    and the bytes must not appear anywhere in the response."""
    sentinel = "sk_live_this_must_never_be_echoed_0123456789"
    monkeypatch.setattr(get_settings(), "stripe_secret_key", sentinel)

    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/env", headers=auth_headers)

    body = assert_envelope_ok(resp)
    stripe = next(v for v in body["variables"] if v["key"] == "STRIPE_SECRET_KEY")
    assert stripe["secret"] is True
    assert stripe["is_set"] is True
    assert stripe["value"] is None
    assert stripe["length"] == len(sentinel)  # presence + length, nothing more
    assert sentinel not in resp.text


@pytest.mark.asyncio
async def test_env_withholds_every_secret_it_declares(
    client, created_user, auth_headers
):
    """Whatever this machine happens to have configured, no variable flagged
    ``secret`` may carry a value — the masking is driven by the registry, not
    by which keys happen to be blank in the test environment."""
    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/env", headers=auth_headers)

    body = assert_envelope_ok(resp)
    secrets = [v for v in body["variables"] if v["secret"]]
    assert secrets, "the registry declares secrets; the report must carry them"
    assert all(v["value"] is None for v in secrets)


@pytest.mark.asyncio
async def test_env_withholds_the_admin_allowlist_because_it_is_pii(
    client, created_user, auth_headers
):
    """ADMIN_EMAILS is not a credential but it *is* a list of staff addresses,
    so it is classed secret: presence and count, never the addresses."""
    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/env", headers=auth_headers)

    body = assert_envelope_ok(resp)
    allowlist = next(v for v in body["variables"] if v["key"] == "ADMIN_EMAILS")
    assert allowlist["secret"] is True
    assert allowlist["value"] is None
    assert created_user.email not in resp.text


@pytest.mark.asyncio
async def test_env_shows_non_secret_config_verbatim(
    client, created_user, auth_headers, monkeypatch
):
    """The point of the page is reading the live config at a glance, so
    anything browser-safe (regions, model names, the Stripe *publishable* key)
    comes back in full."""
    monkeypatch.setattr(get_settings(), "gcp_region", "europe-west4")
    monkeypatch.setattr(get_settings(), "stripe_publishable_key", "pk_live_browsersafe")

    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/env", headers=auth_headers)

    by_key = {v["key"]: v for v in assert_envelope_ok(resp)["variables"]}
    assert by_key["GCP_REGION"]["value"] == "europe-west4"
    assert by_key["GCP_REGION"]["secret"] is False
    assert by_key["STRIPE_PUBLISHABLE_KEY"]["value"] == "pk_live_browsersafe"


# ── GET /v1/admin/integrations ──
class _StubResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _StubHttpClient:
    """Records every probe instead of leaving the process."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.urls: list[str] = []

    async def get(self, url: str, timeout: float | None = None) -> _StubResponse:
        self.urls.append(url)
        return _StubResponse(self.status_code)


def _stub_probe_client(monkeypatch, client_stub: _StubHttpClient) -> None:
    async def _get_client() -> _StubHttpClient:
        return client_stub

    monkeypatch.setattr(
        "app.services.admin.integration_service.get_http_client", _get_client
    )


@pytest.mark.asyncio
async def test_integrations_default_view_makes_no_outbound_calls(
    client, created_user, auth_headers, monkeypatch
):
    """Opening the page must be free. Probing is opt-in, so the default render
    can't fan out to a dozen third parties (or hang on one that is down)."""
    stub = _StubHttpClient()
    _stub_probe_client(monkeypatch, stub)

    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/integrations", headers=auth_headers)

    body = assert_envelope_ok(resp)
    assert body["probed"] is False
    assert stub.urls == []
    # Nothing may claim to be reachable when nothing was contacted.
    assert all(i["status"] != "live" for i in body["integrations"])
    assert {"stripe", "resend", "anthropic", "gcp"} <= {
        i["id"] for i in body["integrations"]
    }


@pytest.mark.asyncio
async def test_integrations_probe_marks_reachable_services_live(
    client, created_user, auth_headers, monkeypatch
):
    """``probe=true`` pings each configured service. Any HTTP answer counts as
    reachable — a 401 from a paid API still proves the network path works."""
    stub = _StubHttpClient(status_code=401)
    _stub_probe_client(monkeypatch, stub)

    with _as_admin(created_user.email):
        resp = await client.get(
            "/v1/admin/integrations?probe=true", headers=auth_headers
        )

    body = assert_envelope_ok(resp)
    assert body["probed"] is True
    assert stub.urls, "a configured service with a probe URL should be pinged"
    # Scryfall is keyless, so it is always configured and always probed.
    scryfall = next(i for i in body["integrations"] if i["id"] == "scryfall")
    assert scryfall["status"] == "live"
    assert scryfall["http_status"] == 401
    assert scryfall["latency_ms"] is not None


@pytest.mark.asyncio
async def test_integrations_probe_reports_an_unreachable_service_as_down(
    client, created_user, auth_headers, monkeypatch
):
    """A transport failure is the only thing that means 'down', and it must be
    reported per-service rather than failing the whole report."""

    class _DeadClient(_StubHttpClient):
        async def get(self, url: str, timeout: float | None = None) -> _StubResponse:
            raise ConnectionError("dns failure")

    _stub_probe_client(monkeypatch, _DeadClient())

    with _as_admin(created_user.email):
        resp = await client.get(
            "/v1/admin/integrations?probe=true", headers=auth_headers
        )

    body = assert_envelope_ok(resp)
    scryfall = next(i for i in body["integrations"] if i["id"] == "scryfall")
    assert scryfall["status"] == "down"
    assert "ConnectionError" in scryfall["detail"]
    assert scryfall["http_status"] is None


@pytest.mark.asyncio
async def test_integrations_report_carries_no_credentials(
    client, created_user, auth_headers, monkeypatch
):
    """The catalog answers *whether* a key is configured, never what it is."""
    sentinel = "apitcg_key_must_not_leak_9876543210"
    monkeypatch.setattr(get_settings(), "apitcg_api_key", sentinel)

    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/integrations", headers=auth_headers)

    body = assert_envelope_ok(resp)
    apitcg = next(i for i in body["integrations"] if i["id"] == "apitcg")
    assert apitcg["configured"] is True
    assert sentinel not in resp.text
