"""
Worker job: refresh market/pricing data for all cards in the catalog.
This runs on a schedule (or manually) and updates the DB so API endpoints
can serve instantly from cache, not by fanning out to slow providers.
"""

import asyncio
import logging

from app.platform.db import get_session
from app.services.catalog.card_refresh_utils import (
    get_all_card_ids,
    refresh_market_data,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 100


async def refresh_all_cards():
    async with get_session() as session:
        card_ids = await get_all_card_ids(session)
        for i in range(0, len(card_ids), BATCH_SIZE):
            batch = card_ids[i : i + BATCH_SIZE]
            results = await asyncio.gather(
                *[refresh_market_data(card_id, session) for card_id in batch]
            )
            await session.commit()
            logger.info(
                "Refreshed %d/%d cards (success: %d)",
                i + len(batch),
                len(card_ids),
                sum(results),
            )


if __name__ == "__main__":
    asyncio.run(refresh_all_cards())
