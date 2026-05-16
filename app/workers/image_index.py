"""Catalog image-index worker.

Walks ``cards`` rows that have an ``image_url`` but no perceptual hash on
file, downloads each image, computes a pHash/dHash, and persists the result
into ``Card.card_metadata['image_hash']``.  Once indexed, a scan can be
matched to a catalog card by Hamming distance (see
:func:`card_resolver_service.resolve_by_phash`).

Bounded by ``batch_size`` and rate-limited so we don't hammer Pokémon TCG /
Scryfall / YGOPRODeck CDNs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models.card import Card
from app.services import fingerprint_service
from app.utils.logger import get_logger

logger = get_logger("workers.image_index")

DEFAULT_BATCH_SIZE = 100
_INTER_CALL_DELAY_SEC = 0.20


async def index_card_images(
    ctx: dict[str, Any] | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    force: bool = False,
) -> dict[str, int]:
    """Index up to ``batch_size`` cards. Returns counters."""
    scanned = 0
    updated = 0
    missed = 0

    sm = get_sessionmaker()
    async with sm() as session:
        stmt = (
            select(Card)
            .where(Card.image_url.is_not(None))
            .order_by(Card.updated_at.asc())
            .limit(batch_size)
        )
        rows = (await session.execute(stmt)).scalars().all()

        for row in rows:
            scanned += 1
            meta = row.card_metadata if isinstance(row.card_metadata, dict) else {}
            if not force and meta.get("image_hash"):
                continue
            image_url = (meta.get("image_url") or row.image_url) if meta else row.image_url
            if not image_url:
                continue

            fp = await fingerprint_service.fingerprint_from_image_url(image_url)
            if fp is None:
                missed += 1
            else:
                new_meta = dict(meta)
                new_meta["image_hash"] = {
                    "phash": fp.phash,
                    "dhash": fp.dhash,
                    "hash_size": 16,
                }
                row.card_metadata = new_meta
                updated += 1

            await asyncio.sleep(_INTER_CALL_DELAY_SEC)

        if updated:
            await session.commit()

    result = {"scanned": scanned, "updated": updated, "missed": missed}
    logger.info("image_index complete: %s", result)
    return result


__all__ = ["index_card_images"]
