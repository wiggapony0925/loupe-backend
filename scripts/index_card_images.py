"""Cloud Run Job / CLI entrypoint for the catalog image-hash backfill.

Walks the **entire** ``cards`` table once, computing a perceptual hash for
every card that has an ``image_url`` but no ``image_phash`` yet, so live
scans can be matched to catalog art by Hamming distance (see
:func:`app.services.catalog.card_resolver_service.resolve_catalog_by_phash`).

Unlike the daily ``image_index`` cron (which drains 100 rows/run and needs
the arq worker + Redis running), this script talks to the database directly
and is safe to run as a one-off Cloud Run Job::

    python -m scripts.index_card_images

It pages with a stable ``offset`` sweep, so it skips already-hashed rows
cheaply and never loops forever on un-hashable images.

Options (env vars):
    INDEX_BATCH_SIZE   rows fetched per page         (default 100)
    INDEX_MAX_BATCHES  safety cap on pages, 0 = all  (default 0)
"""

from __future__ import annotations

import asyncio
import json
import os

from app.tasks.image_index import index_card_images
from app.utils.logger import get_logger

logger = get_logger("scripts.index_card_images")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using %d", name, raw, default)
        return default


async def main() -> None:
    batch_size = _int_env("INDEX_BATCH_SIZE", 100)
    max_batches = _int_env("INDEX_MAX_BATCHES", 0)

    offset = 0
    batches = 0
    totals = {"scanned": 0, "updated": 0, "missed": 0}

    while True:
        result = await index_card_images(
            batch_size=batch_size, stable=True, offset=offset
        )
        scanned = result["scanned"]
        if scanned == 0:
            break

        for key in totals:
            totals[key] += result[key]
        offset += scanned
        batches += 1

        logger.info(
            "image_index batch %d (offset=%d) %s",
            batches,
            offset,
            json.dumps(result),
        )

        if max_batches and batches >= max_batches:
            logger.info("image_index hit INDEX_MAX_BATCHES=%d, stopping", max_batches)
            break

    logger.info("image_index backfill complete %s", json.dumps(totals))


if __name__ == "__main__":
    asyncio.run(main())
