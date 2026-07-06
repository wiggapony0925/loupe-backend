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
from app.services.catalog import card_search_service, catalog_browse_service
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


def _alt_art_url(url: str) -> str | None:
    """The upstream's alternate scan of the same card, when one exists.

    pokemontcg.io serves ``<n>.png`` (small) and ``<n>_hires.png`` — two
    DIFFERENT scans whose pHashes can differ by 40+ bits. Index both so a
    scan matching either variant resolves.
    """
    if "images.pokemontcg.io" in url and url.endswith(".png") and "_hires" not in url:
        return url[: -len(".png")] + "_hires.png"
    return None


async def _hash_one(card: Any, sem: asyncio.Semaphore) -> dict[str, Any] | None:
    upstream_id = _field(card, "id")
    url = _image_url(card)
    if not upstream_id or not url:
        return None
    async with sem:
        fp = await fingerprint_from_image_url(url)
    if fp is None or not fp.phash:
        return None
    alt_url = _alt_art_url(url)
    fp_alt = None
    if alt_url:
        async with sem:
            fp_alt = await fingerprint_from_image_url(alt_url)
    return {
        "upstream_id": upstream_id,
        "tcg": (_field(card, "tcg") or "").lower() or "unknown",
        "name": _field(card, "name") or "",
        "set_name": _field(card, "set_name"),
        "number": _field(card, "number"),
        "image_url": url,
        "phash": fp.phash,
        "dhash": fp.dhash,
        "phash_alt": fp_alt.phash if fp_alt else None,
        "dhash_alt": fp_alt.dhash if fp_alt else None,
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
                "phash_alt": stmt.excluded.phash_alt,
                "dhash_alt": stmt.excluded.dhash_alt,
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

        i, s, f = await _process_cards(sm, sem, cards, force)
        indexed += i
        skipped += s
        failed += f
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


async def _db_with_retry(sm, work, *, attempts: int = 4):
    """Run ``work(session)`` in a fresh session, retrying transient connection
    drops (a multi-hour job outlives the Cloud SQL proxy's idle timeout, which
    surfaces as ``asyncpg InterfaceError: connection is closed``)."""
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            async with sm() as session:
                return await work(session)
        except (InterfaceError, OperationalError, DBAPIError) as exc:
            last = exc
            logger.warning(
                "db connection dropped (attempt %d/%d) — retrying: %s",
                attempt + 1,
                attempts,
                str(exc)[:120],
            )
            await asyncio.sleep(2.0 * (attempt + 1))
    raise last if last else RuntimeError("db retry exhausted")


async def _process_cards(
    sm, sem: asyncio.Semaphore, cards: list, force: bool
) -> tuple[int, int, int]:
    """Hash one page of cards (network), then upsert with connection-drop
    retry. Returns (indexed, skipped, failed)."""

    # Hashing (image downloads) is done ONCE up front against a first session
    # only for the de-dupe read, so a DB retry never re-downloads.
    async def _dedupe(session):
        ids = [c for c in (_field(x, "id") for x in cards) if c]
        already = set() if force else await _existing_ids(session, ids)
        return already

    already = await _db_with_retry(sm, _dedupe)
    todo = [c for c in cards if _field(c, "id") not in already]
    skipped = len(cards) - len(todo)
    results = await asyncio.gather(*(_hash_one(c, sem) for c in todo))
    records = [r for r in results if r is not None]
    failed = len(todo) - len(records)

    async def _write(session):
        await _upsert(session, records)

    if records:
        await _db_with_retry(sm, _write)
    return len(records), skipped, failed


async def index_game_by_set(
    game: str,
    *,
    page_size: int,
    concurrency: int,
    force: bool,
    only_sets: set[str] | None = None,
) -> dict[str, int]:
    """Index a game set-by-set — the ONLY way to reach 100% coverage when the
    upstream caps global pagination (pokemontcg.io 400s past ~page 55). Each
    set is shallow (<500 cards) so no deep-pagination wall.

    ``only_sets`` (bare codes like ``base1`` or full ids) narrows the walk —
    the tool for indexing a just-released set without re-walking everything.
    """
    sm = get_sessionmaker()
    sem = asyncio.Semaphore(concurrency)
    indexed = skipped = failed = 0
    # list_sets rides the same flaky upstream as everything else — retry a few
    # times before concluding the game has no sets.
    sets: list = []
    for attempt in range(4):
        sets_body = await card_search_service.list_sets(game)
        sets = sets_body.get("results") or []
        if sets:
            break
        await asyncio.sleep(2.0 * (attempt + 1))
    if only_sets:
        wanted = {s.lower() for s in only_sets}
        sets = [
            st
            for st in sets
            if (st.get("id") or "").lower() in wanted
            or (st.get("id") or "").lower().split(":")[-1] in wanted
            or (st.get("code") or "").lower() in wanted
        ]
    logger.info("[%s] by-set: %d sets", game, len(sets))
    for st in sets:
        set_id = st.get("id") if isinstance(st, dict) else getattr(st, "id", None)
        set_name = st.get("name") if isinstance(st, dict) else getattr(st, "name", None)
        if not set_id:
            continue
        page = 1
        while True:
            result = await catalog_browse_service.browse_catalog(
                game, page, page_size, "name", set_id=set_id
            )
            cards = result.get("cards") or []
            if not cards:
                break
            total = int(result.get("total") or 0)
            i, s, f = await _process_cards(sm, sem, cards, force)
            indexed += i
            skipped += s
            failed += f
            if total and page * page_size >= total:
                break
            page += 1
        logger.info(
            "[%s] set %s — indexed=%d skipped=%d failed=%d",
            game,
            set_name or set_id,
            indexed,
            skipped,
            failed,
        )
    return {"game": game, "indexed": indexed, "skipped": skipped, "failed": failed}  # type: ignore[dict-item]


async def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill catalog perceptual-hash index")
    ap.add_argument("--games", default=",".join(DEFAULT_GAMES))
    ap.add_argument("--page-size", type=int, default=250)
    ap.add_argument("--max-pages", type=int, default=0, help="0 = all pages")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--start-page", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--by-set",
        action="store_true",
        help="Walk set-by-set for 100%% coverage (required for Pokémon — the "
        "upstream 400s past ~page 55 of global pagination).",
    )
    ap.add_argument(
        "--sets",
        default=None,
        help="Comma-separated set codes/ids to index (implies --by-set) — "
        "e.g. a just-released set, without re-walking the whole game.",
    )
    args = ap.parse_args()

    only_sets = (
        {s.strip() for s in args.sets.split(",") if s.strip()} if args.sets else None
    )
    games = [g.strip() for g in args.games.split(",") if g.strip()]
    for game in games:
        if args.by_set or only_sets:
            summary = await index_game_by_set(
                game,
                page_size=args.page_size,
                concurrency=args.concurrency,
                force=args.force,
                only_sets=only_sets,
            )
        else:
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
