"""Async SQLAlchemy engine + session factory and FastAPI dependency."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


class DatabaseConfigError(RuntimeError):
    """Raised when ``DATABASE_URL`` is configured in a way that cannot work.

    Exists to turn a cryptic, deep-in-asyncpg ``ConnectionResetError:
    [Errno 54] Connection reset by peer`` (what you get when you point the
    app at the Cloud SQL *public IP* over TCP, which has no authorized
    networks) into an actionable message at startup.
    """


def _validate_database_url(url: str, *, cloud_sql_connection_name: str | None) -> None:
    """Fail fast on DSNs that will only error later in a confusing way.

    The one footgun we guard against: a Postgres/asyncpg DSN whose host is a
    raw IP address. In this project Postgres is reached either via the local
    Docker container (``localhost``) or the Cloud SQL unix socket
    (``?host=/cloudsql/<instance>``) — never the Cloud SQL public IP, which
    has no ``authorizedNetworks`` and resets the TCP/SSL handshake.
    """
    if "postgres" not in url:
        return

    split = urlsplit(url)
    # Unix-socket DSNs encode the path in the query (``?host=/cloudsql/...``)
    # and have no network host — those are always fine.
    if "host=/cloudsql/" in url or "host=/" in url:
        return

    host = split.hostname or ""
    if not _IPV4_RE.match(host):
        return  # hostnames (localhost, db.example.com, proxy) are fine

    # Raw-IP host on a Postgres DSN. If a Cloud SQL instance is configured,
    # this is almost certainly the public IP and will be reset.
    hint = (
        "Connect via the local Docker Postgres "
        "(postgresql+asyncpg://loupe:loupe@localhost:5433/loupe), the Cloud SQL "
        "unix socket (...?host=/cloudsql/<instance>), or the Cloud SQL Auth "
        "Proxy on localhost — not the Cloud SQL public IP (no authorized "
        "networks; it resets the connection)."
    )
    if cloud_sql_connection_name:
        raise DatabaseConfigError(
            f"DATABASE_URL points at a raw IP host ({host}) while a Cloud SQL "
            f"instance ({cloud_sql_connection_name}) is configured. {hint}"
        )


def _build_engine(url: str) -> AsyncEngine:
    """Construct an async engine with sane pool defaults per dialect."""
    kwargs: dict[str, Any] = {"future": True, "echo": False, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs.pop("pool_pre_ping", None)
        # In-memory sqlite needs a single shared connection so all sessions
        # see the same database (e.g. for the test suite).
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
    return create_async_engine(url, **kwargs)


def get_engine() -> AsyncEngine:
    """Return the lazily-initialised process-wide async engine."""
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        _validate_database_url(
            settings.database_url,
            cloud_sql_connection_name=settings.cloud_sql_connection_name,
        )
        _engine = _build_engine(settings.database_url)
        _sessionmaker = async_sessionmaker(
            _engine,
            expire_on_commit=False,
            autoflush=False,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the cached :class:`async_sessionmaker`."""
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields an :class:`AsyncSession`.

    The session is closed automatically when the request finishes. Callers
    are responsible for committing — this dependency does not auto-commit.
    """
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def reset_engine() -> None:
    """Dispose the current engine — useful in tests when DB URL changes."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


__all__ = [
    "DatabaseConfigError",
    "get_db",
    "get_engine",
    "get_sessionmaker",
    "reset_engine",
]
