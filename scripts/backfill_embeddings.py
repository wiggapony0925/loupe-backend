"""Backfill CNN image embeddings for every catalog card → pgvector.

Run ONCE (and after big catalog syncs) to populate ``catalog_card_embeddings``
so the learned-embedding matcher has something to search. Requires the ONNX
model present (``CARD_EMBED_MODEL_PATH``) and pgvector installed
(migration 0037).

    python -m scripts.backfill_embeddings [--limit N] [--only-missing]

It streams cards in batches, pulls each card's art URL from its stored upstream
payload, downloads + encodes it, and upserts the vector. Safe to re-run;
``--only-missing`` skips cards already embedded.
"""

from __future__ import annotations

import argparse
import asyncio

import httpx
from sqlalchemy import select, text

from app.db import get_sessionmaker
from app.models.catalog_mirror import CatalogMirrorCard
from app.services.identification.card_image_encoder import (
    embed_image_bytes,
    encoder_available,
)
from app.utils.logger import get_logger

logger = get_logger("scripts.backfill_embeddings")

MODEL_TAG = "clip-onnx-v1"


def _image_url(payload: dict) -> str | None:
    """Best card-art URL from an upstream payload (pokemontcg / scryfall / ygo)."""
    if not isinstance(payload, dict):
        return None
    images = payload.get("images")
    if isinstance(images, dict):
        for key in ("large", "png", "normal", "small"):
            val = images.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
    for key in ("image_url", "imageUrl", "image"):
        val = payload.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    return None


async def _embedded_ids() -> set[str]:
    async with get_sessionmaker()() as db:
        rows = await db.execute(text("SELECT card_id FROM catalog_card_embeddings"))
        return {r[0] for r in rows.all()}


async def run(limit: int | None, only_missing: bool) -> None:
    if not encoder_available():
        raise SystemExit(
            "No ONNX encoder loaded — set CARD_EMBED_MODEL_PATH to the model file."
        )
    done: set[str] = await _embedded_ids() if only_missing else set()
    embedded = skipped = failed = 0

    async with (
        httpx.AsyncClient(timeout=30.0) as http,
        get_sessionmaker()() as db,
    ):
        stmt = select(CatalogMirrorCard.id, CatalogMirrorCard.payload)
        if limit:
            stmt = stmt.limit(limit)
        result = await db.stream(stmt)
        async for card_id, payload in result:
            if card_id in done:
                skipped += 1
                continue
            url = _image_url(payload or {})
            if not url:
                skipped += 1
                continue
            try:
                resp = await http.get(url)
                resp.raise_for_status()
                vec = embed_image_bytes(resp.content)
            except Exception as exc:
                logger.warning("backfill: %s failed (%s)", card_id, exc)
                failed += 1
                continue
            if vec is None:
                failed += 1
                continue
            await db.execute(
                text(
                    "INSERT INTO catalog_card_embeddings (card_id, embedding, model) "
                    "VALUES (:id, :emb, :model) "
                    "ON CONFLICT (card_id) DO UPDATE SET "
                    "embedding = EXCLUDED.embedding, model = EXCLUDED.model, "
                    "updated_at = now()"
                ),
                {"id": card_id, "emb": str(vec), "model": MODEL_TAG},
            )
            embedded += 1
            if embedded % 200 == 0:
                await db.commit()
                logger.info("backfill: %s embedded…", embedded)
        await db.commit()

    logger.info(
        "backfill done: embedded=%s skipped=%s failed=%s", embedded, skipped, failed
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only-missing", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.limit, args.only_missing))


if __name__ == "__main__":
    main()
