"""arq worker configuration and scheduled tasks."""

from __future__ import annotations

from typing import Any

try:
    from arq.connections import RedisSettings
    from arq.cron import cron
except ImportError:  # pragma: no cover - arq optional at import time
    RedisSettings = None  # type: ignore[assignment,misc]
    cron = None  # type: ignore[assignment]

from app.config import get_settings
from app.utils.logger import get_logger

_log = get_logger("worker")


async def startup(ctx: dict[str, Any]) -> None:
    """arq startup hook — eagerly construct DB engine."""
    from app.db import get_engine

    get_engine()
    _log.info("arq worker ready")


async def shutdown(ctx: dict[str, Any]) -> None:
    """arq shutdown hook — close shared resources."""
    from app.db import reset_engine
    from app.platform.redis_client import close_redis

    await reset_engine()
    await close_redis()
    _log.info("arq worker stopped")


async def process_scan(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Process a finished scan-upload through the grading pipeline."""
    from app.tasks.scan_processor import process_scan as run_scan_processor

    await run_scan_processor(payload)
    return {"ok": True}


async def catalog_sync(ctx: dict[str, Any]) -> dict[str, Any]:
    """Pull recent card data from upstream catalogs."""
    from app.tasks.catalog_sync import catalog_sync as run_catalog_sync

    await run_catalog_sync(ctx)
    return {"ok": True}


async def price_backfill(ctx: dict[str, Any]) -> dict[str, Any]:
    """Backfill embedded upstream prices into local ``cards.metadata``."""
    from app.tasks.price_backfill import backfill_prices

    return await backfill_prices(ctx)


async def image_index(ctx: dict[str, Any]) -> dict[str, Any]:
    """Compute perceptual hashes for catalog card images."""
    from app.tasks.image_index import index_card_images

    return await index_card_images(ctx)


async def price_snapshot(ctx: dict[str, Any]) -> dict[str, Any]:
    """Append today's live market price to each card's price_history."""
    from app.tasks.price_snapshot import snapshot_prices

    return await snapshot_prices(ctx)


async def cache_warm(ctx: dict[str, Any]) -> dict[str, Any]:
    """Keep the home screen's 15-minute caches from ever being cold."""
    from app.tasks.cache_warm import warm_home_caches

    return await warm_home_caches(ctx)


async def portfolio_digest(ctx: dict[str, Any]) -> dict[str, Any]:
    """Weekly collection recap to every subscribed user."""
    from app.tasks.lifecycle_email import send_portfolio_digests

    return await send_portfolio_digests(ctx)


async def pro_expiry_notices(ctx: dict[str, Any]) -> dict[str, Any]:
    """Daily heads-up for Pro memberships about to lapse."""
    from app.tasks.lifecycle_email import send_pro_expiry_notices

    return await send_pro_expiry_notices(ctx)


def _redis_settings() -> Any:
    if RedisSettings is None:  # pragma: no cover
        return None
    settings = get_settings()
    return RedisSettings.from_dsn(settings.redis_url)


class WorkerSettings:
    """arq :class:`WorkerSettings` declarative class.

    Launch with::

        arq app.worker.WorkerSettings
    """

    functions = [
        process_scan,
        catalog_sync,
        price_backfill,
        price_snapshot,
        image_index,
        cache_warm,
        portfolio_digest,
        pro_expiry_notices,
    ]
    cron_jobs = (
        [
            cron(catalog_sync, hour={3}, minute={0}),
            cron(price_backfill, hour={4}, minute={0}),
            # Snapshot AFTER price_backfill so today's row reflects
            # whatever upstream refresh landed at 04:00.
            cron(price_snapshot, hour={4}, minute={30}),
            cron(image_index, hour={5}, minute={0}),
            # Every 10 minutes, against a 15-minute TRENDING_TTL. This is the
            # only sub-daily job here, and it is not really a background task:
            # it is the difference between the home screen's rails costing
            # 250ms and costing 1.2 seconds (7.6s at the tail), measured from
            # production logs. With one real user the cache has always expired
            # before anyone arrives, so without this the warm path is the one
            # nobody ever takes.
            cron(cache_warm, minute={0, 10, 20, 30, 40, 50}),
            # Expiry notices run after the nightly price work so a Pro user's
            # last emails aren't built on yesterday's numbers.
            cron(pro_expiry_notices, hour={13}, minute={0}),
            # Monday mid-morning UTC — the digest reports the week just ended,
            # and lands in business hours across US/EU rather than overnight.
            cron(portfolio_digest, weekday={0}, hour={14}, minute={0}),
        ]
        if cron is not None
        else []
    )
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    max_jobs = 4
    job_timeout = 300


__all__ = [
    "WorkerSettings",
    "catalog_sync",
    "image_index",
    "portfolio_digest",
    "price_backfill",
    "price_snapshot",
    "pro_expiry_notices",
    "process_scan",
    "shutdown",
    "startup",
]
