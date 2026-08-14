"""Application lifecycle: startup / shutdown hooks and background supervisor."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import FastAPI

from app.utils.logger import get_logger

_log = get_logger("lifecycle")


@dataclass
class AppState:
    """Mutable runtime state shared across the process."""

    warmup_complete: bool = False
    background_tasks: list[asyncio.Task[None]] = field(default_factory=list)


_app_state = AppState()


def get_app_state() -> AppState:
    """Return the shared :class:`AppState` singleton."""
    return _app_state


def is_warmed_up() -> bool:
    """Whether startup has finished initialising critical subsystems."""
    return _app_state.warmup_complete


async def _resilient_loop(
    name: str,
    coro_factory: Callable[[], Awaitable[None]],
    *,
    max_restarts: int = 50,
    base_delay: float = 1.0,
) -> None:
    """Run an async task forever; restart on failure with exponential backoff."""
    restarts = 0
    while restarts < max_restarts:
        try:
            await coro_factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            restarts += 1
            delay = min(base_delay * (2 ** (restarts - 1)), 60.0)
            _log.warning(
                "background task %s crashed (attempt %d/%d): %s — retrying in %.1fs",
                name,
                restarts,
                max_restarts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    _log.error("background task %s exceeded max restarts; giving up", name)


def _spawn(name: str, coro_factory: Callable[[], Awaitable[None]]) -> None:
    task = asyncio.create_task(_resilient_loop(name, coro_factory), name=name)
    _app_state.background_tasks.append(task)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """ASGI lifespan: initialise resources on startup, tear them down on exit."""
    _log.info("loupe-backend starting up")
    try:
        # Eagerly construct the DB engine so the first request doesn't pay the cost.
        from app.db import get_engine

        get_engine()
    except Exception as exc:
        _log.warning("db engine init deferred: %s", exc)

    # Auto-generate monthly + yearly portfolio statements (Amex-style).
    # No-op when ``reports_scheduler_enabled`` is false or in tests.
    try:
        from app.config import get_settings
        from app.services.analytics.reports import scheduler_loop

        s = get_settings()
        if s.reports_scheduler_enabled and not s.is_test:
            _spawn("reports-scheduler", scheduler_loop)
    except Exception as exc:
        _log.warning("reports scheduler not spawned: %s", exc)

    # Ensure every developer-portal page has a (DB-backed) feature flag so it's
    # togglable from the Feature flags page. Idempotent + non-destructive; skip
    # in tests, and never let it block startup.
    try:
        from app.config import get_settings
        from app.db.session import get_sessionmaker
        from app.services.admin.flag_seed import (
            grant_admin_to_allowlisted,
            seed_admin_flags,
        )

        if not get_settings().is_test:
            async with get_sessionmaker()() as db:
                await seed_admin_flags(db)
                # Write the ADMIN_EMAILS bootstrap down as a real grant, so the
                # database agrees with who can actually reach /v1/admin and a
                # cleared env var cannot leave the app with no admins at all.
                await grant_admin_to_allowlisted(db)
    except Exception as exc:
        _log.warning("admin flag seed skipped: %s", exc)

    # Give Community someone to follow on day one. An empty "Suggested for
    # you" reads as a broken feature, not as an early one. Idempotent and
    # non-destructive; never blocks startup.
    try:
        from app.config import get_settings
        from app.db.session import get_sessionmaker
        from app.social.seed import seed_demo_collectors

        if not get_settings().is_test:
            async with get_sessionmaker()() as db:
                await seed_demo_collectors(db)
    except Exception as exc:
        _log.warning("demo collector seed skipped: %s", exc)

    # Warm the catalog art-hash index in the background so the FIRST scan on
    # a fresh instance doesn't pay the ~130k-row load. Never blocks startup.
    async def _warm_hash_index() -> None:
        try:
            from app.db.session import get_sessionmaker
            from app.services.catalog import catalog_hash_index

            async with get_sessionmaker()() as db:
                await catalog_hash_index.warm(db)
        except Exception as exc:
            _log.warning("catalog hash index warm skipped: %s", exc)

    try:
        from app.config import get_settings as _gs

        if not _gs().is_test:
            warm_task = asyncio.create_task(_warm_hash_index(), name="hash-index-warm")
            _app_state.background_tasks.append(warm_task)
    except Exception as exc:
        _log.warning("hash index warm not spawned: %s", exc)

    _app_state.warmup_complete = True
    _log.info("loupe-backend ready")

    try:
        yield
    finally:
        _log.info("loupe-backend shutting down")
        # Flush queued transactional email before tearing anything down —
        # a welcome/security notice queued moments ago should still go out.
        with contextlib.suppress(Exception):
            from app.services import email_service

            await email_service.drain(timeout=10.0)
        for task in _app_state.background_tasks:
            task.cancel()
        for task in _app_state.background_tasks:
            with contextlib.suppress(BaseException):
                await task
        _app_state.background_tasks.clear()

        # Close DB engine.
        with contextlib.suppress(Exception):
            from app.db import reset_engine

            await reset_engine()

        # Close shared HTTP and Redis clients.
        with contextlib.suppress(Exception):
            from app.platform.redis_client import close_redis

            await close_redis()


__all__ = [
    "AppState",
    "get_app_state",
    "is_warmed_up",
    "lifespan",
]
