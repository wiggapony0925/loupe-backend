"""Tests for the ``require_admin`` dependency / admin-gated endpoints."""

from __future__ import annotations

import pytest

from app.config import get_settings


@pytest.mark.asyncio
async def test_ocr_metrics_unauth(client):
    resp = await client.get("/v1/cards/admin/ocr/metrics")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_ocr_metrics_signed_in_non_admin(client, auth_headers):
    # Default admin allowlist is empty in tests → any caller is rejected.
    resp = await client.get("/v1/cards/admin/ocr/metrics", headers=auth_headers)
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["message"] == "Admin privileges required"


@pytest.mark.asyncio
async def test_ocr_metrics_admin_allowed(client, created_user, auth_headers):
    """Bypass: drop the admin's email into the allowlist mid-test."""
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = created_user.email  # type: ignore[misc]
    try:
        resp = await client.get("/v1/cards/admin/ocr/metrics", headers=auth_headers)
        assert resp.status_code == 200
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


@pytest.mark.asyncio
async def test_admin_email_set_parsing():
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = "  A@x.com, b@x.com ,, B@x.com "  # type: ignore[misc]
    try:
        assert settings.admin_email_set == {"a@x.com", "b@x.com"}
    finally:
        settings.admin_emails = prev  # type: ignore[misc]
