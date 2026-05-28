"""Cloud Run Job entrypoint for the daily price snapshot.

Thin CLI wrapper around :func:`app.tasks.price_snapshot.snapshot_prices`.
Mirrors the pattern used by :mod:`scripts.seed_sealed_products` so the
Cloud Run Job invocation is uniform (`python -m scripts.snapshot_prices`).

Usage:
    python -m scripts.snapshot_prices
"""

from __future__ import annotations

import asyncio
import json

from app.tasks.price_snapshot import snapshot_prices
from app.utils.logger import get_logger

logger = get_logger("scripts.snapshot_prices")


async def main() -> None:
    result = await snapshot_prices()
    # Emit a single structured line so Cloud Logging can index it.
    logger.info("price_snapshot_result %s", json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
