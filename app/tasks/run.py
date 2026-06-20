"""Redis-free entrypoint for the ingestion pipeline (Cloud Run Job + Scheduler).

The arq worker (:mod:`app.worker`) needs Redis to run its cron jobs, and prod
has no Redis right now, so the worker crash-loops and nothing ingests. This
module runs the *same* task coroutines directly — no queue, no Redis — so a
Cloud Run **Job** triggered by **Cloud Scheduler** can drive the daily pipeline.

Usage::

    python -m app.tasks.run all              # full pipeline, in order
    python -m app.tasks.run price_snapshot   # a single task

Tasks (and the order ``all`` runs them, so each sees the previous step's
writes — mirrors the arq cron schedule):

1. ``catalog_sync``    — pull recent cards from upstream catalogs.
2. ``price_backfill``  — resolve + persist pricing; *fires due price alerts*.
3. ``price_snapshot``  — append today's market price to price history.
4. ``image_index``     — compute perceptual hashes for new card images.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from app.utils.logger import get_logger

logger = get_logger("tasks.run")


async def _catalog_sync() -> dict[str, Any]:
    from app.tasks.catalog_sync import catalog_sync

    return await catalog_sync({})


async def _price_backfill() -> dict[str, Any]:
    from app.tasks.price_backfill import backfill_prices

    return await backfill_prices({})


async def _price_snapshot() -> dict[str, Any]:
    from app.tasks.price_snapshot import snapshot_prices

    return await snapshot_prices({})


async def _image_index() -> dict[str, Any]:
    from app.tasks.image_index import index_card_images

    return await index_card_images({})


#: name → coroutine factory. Lazy imports keep module load cheap + arq-free.
RUNNERS: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
    "catalog_sync": _catalog_sync,
    "price_backfill": _price_backfill,
    "price_snapshot": _price_snapshot,
    "image_index": _image_index,
}

#: Order ``all`` executes in — later steps depend on earlier writes.
PIPELINE: tuple[str, ...] = (
    "catalog_sync",
    "price_backfill",
    "price_snapshot",
    "image_index",
)


async def run_task(name: str) -> dict[str, Any]:
    """Run one task by name, or the whole pipeline when ``name == "all"``.

    Each task is isolated: a failure in one is logged and recorded but does not
    abort the rest of an ``all`` run, so a flaky upstream can't block the
    snapshot/alerts steps. Raises :class:`ValueError` for an unknown name.
    """
    if name == "all":
        results: dict[str, Any] = {}
        for step in PIPELINE:
            results[step] = await _run_one(step, swallow=True)
        return results
    return await _run_one(name, swallow=False)


async def _run_one(name: str, *, swallow: bool) -> dict[str, Any]:
    runner = RUNNERS.get(name)
    if runner is None:
        raise ValueError(
            f"unknown task '{name}'; choose one of {', '.join(RUNNERS)} or 'all'"
        )
    logger.info("ingest task start: %s", name)
    try:
        result = await runner()
    except Exception as exc:
        logger.exception("ingest task failed: %s", name)
        if not swallow:
            raise
        return {"error": str(exc)}
    logger.info("ingest task done: %s -> %s", name, result)
    return result


async def _main(name: str) -> dict[str, Any]:
    try:
        return await run_task(name)
    finally:
        # Dispose the pooled engine so the Job process exits cleanly.
        from app.db import reset_engine

        await reset_engine()


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    name = args[0] if args else "all"
    asyncio.run(_main(name))


if __name__ == "__main__":
    main()
