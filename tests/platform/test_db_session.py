"""Tests for the DATABASE_URL fail-fast guard in app.db.session.

Regression coverage for the production outage where the app was pointed at
the Cloud SQL *public IP* over TCP and asyncpg raised a cryptic
``ConnectionResetError: [Errno 54]`` deep in the SSL handshake. The guard
turns that into a clear ``DatabaseConfigError`` at engine build time.
"""

from __future__ import annotations

import pytest

from app.db.session import DatabaseConfigError, _validate_database_url

_CLOUD_SQL = "loupe-app-56235:us-central1:loupe-pg"


def test_rejects_public_ip_when_cloud_sql_configured() -> None:
    with pytest.raises(DatabaseConfigError) as exc:
        _validate_database_url(
            "postgresql+asyncpg://loupe:pw@34.60.74.1:5432/loupe",
            cloud_sql_connection_name=_CLOUD_SQL,
        )
    assert "34.60.74.1" in str(exc.value)


def test_allows_cloud_sql_unix_socket() -> None:
    _validate_database_url(
        f"postgresql+asyncpg://loupe:pw@/loupe?host=/cloudsql/{_CLOUD_SQL}",
        cloud_sql_connection_name=_CLOUD_SQL,
    )


def test_allows_localhost_docker() -> None:
    _validate_database_url(
        "postgresql+asyncpg://loupe:loupe@localhost:5433/loupe",
        cloud_sql_connection_name=_CLOUD_SQL,
    )


def test_allows_hostname_proxy() -> None:
    _validate_database_url(
        "postgresql+asyncpg://loupe:pw@db.internal:5432/loupe",
        cloud_sql_connection_name=_CLOUD_SQL,
    )


def test_ignores_sqlite() -> None:
    _validate_database_url(
        "sqlite+aiosqlite:///./loupe.db", cloud_sql_connection_name=_CLOUD_SQL
    )


def test_raw_ip_without_cloud_sql_is_allowed() -> None:
    # No Cloud SQL configured → a raw IP is a deliberate self-hosted DB, fine.
    _validate_database_url(
        "postgresql+asyncpg://loupe:pw@10.0.0.5:5432/loupe",
        cloud_sql_connection_name=None,
    )
