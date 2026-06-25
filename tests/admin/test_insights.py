"""Tests for "Ask your data" — focused on the SQL safety guard + gating."""

from __future__ import annotations

import contextlib

import pytest

from app.config import get_settings
from app.services.admin import nl_query_service as nl


@contextlib.contextmanager
def _as_admin(email: str):
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = email  # type: ignore[misc]
    try:
        yield
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


# ── the SQL guard (security-critical) ──
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM users",
        "  select id from cards limit 5  ",
        "WITH t AS (SELECT 1 AS x) SELECT * FROM t",
        "```sql\nSELECT 1\n```",
        "SELECT 1;",  # single trailing semicolon is tolerated
    ],
)
def test_validate_sql_allows_read_only(sql):
    assert nl.validate_sql(sql).lower().startswith(("select", "with"))


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM users",
        "UPDATE users SET plan='pro'",
        "DROP TABLE users",
        "TRUNCATE users",
        "SELECT 1; DROP TABLE users",  # multiple statements
        "WITH x AS (DELETE FROM users RETURNING id) SELECT * FROM x",  # writable CTE
        "INSERT INTO users (email) VALUES ('x')",
        "SET statement_timeout = 0",
    ],
)
def test_validate_sql_blocks_writes(sql):
    with pytest.raises(ValueError):
        nl.validate_sql(sql)


# ── read-only execution ──
@pytest.mark.asyncio
async def test_execute_readonly_returns_rows(db_session, created_user):
    await db_session.commit()
    cols, rows, truncated = await nl._execute_readonly(
        "SELECT count(*) AS n FROM users"
    )
    assert cols == ["n"]
    assert rows[0]["n"] >= 1
    assert truncated is False


# ── configuration + graceful degradation ──
def test_configured_reflects_key():
    settings = get_settings()
    prev = settings.anthropic_api_key
    try:
        settings.anthropic_api_key = ""  # type: ignore[misc]
        assert nl.configured() is False
        settings.anthropic_api_key = "sk-test"  # type: ignore[misc]
        assert nl.configured() is True
    finally:
        settings.anthropic_api_key = prev  # type: ignore[misc]


@pytest.mark.asyncio
async def test_ask_unconfigured_does_not_call_out():
    settings = get_settings()
    prev = settings.anthropic_api_key
    try:
        settings.anthropic_api_key = ""  # type: ignore[misc]
        result = await nl.ask("how many users are there?")
        assert result.configured is False
        assert result.sql is None and result.error
    finally:
        settings.anthropic_api_key = prev  # type: ignore[misc]


# ── endpoint gating ──
@pytest.mark.asyncio
async def test_ask_requires_super_admin(client, created_user, auth_headers, db_session):
    created_user.is_admin = True  # DB admin, not super
    await db_session.commit()
    resp = await client.post(
        "/v1/admin/insights/ask", headers=auth_headers, json={"question": "hi there"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_ask_super_admin_unconfigured(client, created_user, auth_headers):
    with _as_admin(created_user.email):
        resp = await client.post(
            "/v1/admin/insights/ask",
            headers=auth_headers,
            json={"question": "how many users?"},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["configured"] is False
