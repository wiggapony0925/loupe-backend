"""Router + security tests for the admin Operations endpoints.

Verifies admin gating, that the database explorer exposes structure only (never
row data), and that the cloud panel degrades gracefully without GCP config.
"""

from __future__ import annotations

import contextlib

import pytest

from app.config import get_settings


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
