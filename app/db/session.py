"""Async SQLAlchemy engine + session factory and FastAPI dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

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
    "get_db",
    "get_engine",
    "get_sessionmaker",
    "reset_engine",
]
