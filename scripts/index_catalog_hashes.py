"""Backfill the full-catalog perceptual-hash index (``catalog_image_hashes``).

Walks the entire upstream catalog per game, downloads each card's reference
art, computes its pHash/dHash, and upserts a row keyed by the composite
``upstream_id``. Once populated, a live scan matches by artwork alone — instant,
no OCR — for any card (see ``catalog_hash_index.find_nearest``).

Resumable and idempotent: already-indexed ``upstream_id``s are skipped unless
``--force``. Bounded concurrency + a small inter-page delay keep the upstream
providers (pokemontcg.io / Scryfall / YGOPRODeck) happy.

    # everything, all games (the long run)
    PYTHONPATH=. .venv/bin/python scripts/index_catalog_hashes.py

    # one game / a bounded proof
    PYTHONPATH=. .venv/bin/python scripts/index_catalog_hashes.py \
        --games pokemon --max-pages 2 --page-size 50
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import get_sessionmaker
from app.models.catalog_hash import CatalogImageHash
from app.services.catalog import catalog_browse_service
from app.services.catalog.card_fingerprint_service import fingerprint_from_image_url
from app.utils.logger import get_logger

logger = get_logger("scripts.index_catalog_hashes")

DEFAULT_GAMES = ["pokemon", "magic", "yugioh"]


def _field(card: Any, name: str) -> Any:
    """Read a field from a card that may be a dict or a Pydantic model."""
    if isinstance(card, dict):
        return card.get(name)
    return getattr(card, name, None)


def _image_url(card: Any) -> str | None:
    url = _field(card, "image_url")
    if url:
        return url if isinstance(url, str) else _field(url, "url")
    images = _field(card, "images")
    if images:
        for key in ("small", "normal", "large"):
            img = (
                images.get(key)
                if isinstance(images, dict)
                else getattr(images, key, None)
            )
            if img:
                return img if isinstance(img, str) else _field(img, "url")
    return None


async def _existing_ids(session, upstream_ids: list[str]) -> set[str]:
    if not upstream_ids:
        return set()
    rows = (
        (
            await session.execute(
                select(CatalogImageHash.upstream_id).where(
                    CatalogImageHash.upstream_id.in_(upstream_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def _hash_one(card: Any, sem: asyncio.Semaphore) -> dict[str, Any] | None:
    upstream_id = _field(card, "id")
    url = _image_url(card)
    if not upstream_id or not url:
        return None
    async with sem:
        fp = await fingerprint_from_image_url(url)
    if fp is None or not fp.phash:
        return None
    return {
        "upstream_id": upstream_id,
        "tcg": (_field(card, "tcg") or "").lower() or "unknown",
        "name": _field(card, "name") or "",
        "set_name": _field(card, "set_name"),
        "number": _field(card, "number"),
        "image_url": url,
        "phash": fp.phash,
        "dhash": fp.dhash,
    }


async def _upsert(session, records: list[dict[str, Any]]) -> None:
    """Upsert a batch. Postgres ON CONFLICT; SQLite path merges row-by-row."""
    if not records:
        return
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        stmt = pg_insert(CatalogImageHash).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["upstream_id"],
            set_={
                "phash": stmt.excluded.phash,
                "dhash": stmt.excluded.dhash,
                "image_url": stmt.excluded.image_url,
                "name": stmt.excluded.name,
                "set_name": stmt.excluded.set_name,
                "number": stmt.excluded.number,
                "tcg": stmt.excluded.tcg,
            },
        )
        await session.execute(stmt)
    else:
        for rec in records:
            existing = (
                await session.execute(
                    select(CatalogImageHash).where(
                        CatalogImageHash.upstream_id == rec["upstream_id"]
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(CatalogImageHash(**rec))
            else:
                for k, v in rec.items():
                    setattr(existing, k, v)
    await session.commit()


async def index_game(
    game: str,
    *,
    page_size: int,
    max_pages: int,
    concurrency: int,
    force: bool,
    start_page: int,
) -> dict[str, int]:
    sm = get_sessionmaker()
    sem = asyncio.Semaphore(concurrency)
    indexed = skipped = failed = 0
    page = start_page
    empty_streak = 0
    max_page: int | None = None  # set once we learn the catalog total
    while True:
        if max_pages and (page - start_page) >= max_pages:
            break
        if max_page is not None and page > max_page:
            break

        # A page can come back empty from an upstream flake (pokemontcg.io is
        # intermittently slow / returns degraded empty pages) — retry the same
        # page a few times before treating it as a real gap, so a single blip
        # doesn't abort the whole game mid-catalog.
        cards: list = []
        total = 0
        for _attempt in range(3):
            result = await catalog_browse_service.browse_catalog(
                game, page, page_size, "name"
            )
            cards = result.get("cards") or []
            total = int(result.get("total") or 0)
            if total and max_page is None:
                # Walk two pages past the reported end to absorb off-by-one.
                max_page = (total // page_size) + 2
            if cards:
                break
            await asyncio.sleep(1.0)

        if not cards:
            # Stop only after a run of empty pages (the true end / a dead zone).
            empty_streak += 1
            if empty_streak >= 3:
                break
            page += 1
            continue
        empty_streak = 0

        async with sm() as session:
            ids = [c for c in (_field(x, "id") for x in cards) if c]
            already = set() if force else await _existing_ids(session, ids)
            todo = [c for c in cards if _field(c, "id") not in already]
            skipped += len(cards) - len(todo)

            results = await asyncio.gather(*(_hash_one(c, sem) for c in todo))
            records = [r for r in results if r is not None]
            failed += len(todo) - len(records)
            await _upsert(session, records)
            indexed += len(records)

        logger.info(
            "[%s] page %d — indexed=%d skipped=%d failed=%d (total≈%d)",
            game,
            page,
            indexed,
            skipped,
            failed,
            total,
        )
        page += 1
        await asyncio.sleep(0.2)  # be nice to the upstream

    return {"game": game, "indexed": indexed, "skipped": skipped, "failed": failed}  # type: ignore[dict-item]


async def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill catalog perceptual-hash index")
    ap.add_argument("--games", default=",".join(DEFAULT_GAMES))
    ap.add_argument("--page-size", type=int, default=250)
    ap.add_argument("--max-pages", type=int, default=0, help="0 = all pages")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--start-page", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    games = [g.strip() for g in args.games.split(",") if g.strip()]
    for game in games:
        summary = await index_game(
            game,
            page_size=args.page_size,
            max_pages=args.max_pages,
            concurrency=args.concurrency,
            force=args.force,
            start_page=args.start_page,
        )
        logger.info("DONE %s: %s", game, summary)
        print(summary)


if __name__ == "__main__":
    asyncio.run(main())
