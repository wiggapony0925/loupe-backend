"""In-memory perceptual-hash index over the whole catalog.

Backs the scanner's "instant match by artwork" path. The
``catalog_image_hashes`` table holds one pHash per catalog card (backfilled by
``scripts/index_catalog_hashes.py``); this module loads those hashes into a
process-local array once and answers nearest-neighbour queries by Hamming
distance with a single XOR + ``int.bit_count()`` per row — fast enough for a
linear scan of the whole catalog (~130k rows ≈ a few ms) without a vector DB.

The cache refreshes on a TTL *and* when the row count changes (so a running
indexer's new rows become searchable within one TTL). Everything is
best-effort: a load failure yields an empty index and the identifier falls
back to OCR, never a 5xx.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.catalog_hash import CatalogImageHash
from app.utils.logger import get_logger

logger = get_logger("catalog.hash_index")

#: Rebuild the in-memory index at most this often (seconds).
_TTL_SECONDS = 600


@dataclass(frozen=True)
class CatalogHashHit:
    upstream_id: str
    tcg: str
    name: str
    set_name: str | None
    number: str | None
    image_url: str | None
    distance: int
    bits: int

    @property
    def confidence(self) -> float:
        return max(0.0, round(1.0 - (self.distance / self.bits), 3))


# ── Process-local cache ────────────────────────────────────────────────────
# Parallel lists keep it compact: hashes as ints for fast XOR, plus a row of
# denormalized identity so a hit needs no second DB/upstream fetch.
_lock = asyncio.Lock()
_loaded_at: float = 0.0
_row_count: int = -1
_ph_ints: list[int] = []
_rows: list[CatalogHashHit] = []  # distance filled per-query via _clone_with


def _hex_to_int(h: str) -> int | None:
    try:
        return int(h, 16)
    except (ValueError, TypeError):
        return None


async def _refresh_if_stale(db: AsyncSession) -> None:
    global _loaded_at, _row_count, _ph_ints, _rows
    now = time.monotonic()
    if _rows and (now - _loaded_at) < _TTL_SECONDS:
        return
    async with _lock:
        # Re-check inside the lock — another task may have just refreshed.
        if _rows and (time.monotonic() - _loaded_at) < _TTL_SECONDS:
            return
        count = int(
            (await db.execute(select(func.count(CatalogImageHash.id)))).scalar() or 0
        )
        if (
            _rows
            and count == _row_count
            and (time.monotonic() - _loaded_at) < _TTL_SECONDS
        ):
            return
        rows = (
            await db.execute(
                select(
                    CatalogImageHash.upstream_id,
                    CatalogImageHash.tcg,
                    CatalogImageHash.name,
                    CatalogImageHash.set_name,
                    CatalogImageHash.number,
                    CatalogImageHash.image_url,
                    CatalogImageHash.phash,
                )
            )
        ).all()
        ph_ints: list[int] = []
        meta: list[CatalogHashHit] = []
        for upstream_id, tcg, name, set_name, number, image_url, phash in rows:
            val = _hex_to_int(phash)
            if val is None:
                continue
            ph_ints.append(val)
            meta.append(
                CatalogHashHit(
                    upstream_id=upstream_id,
                    tcg=tcg,
                    name=name,
                    set_name=set_name,
                    number=number,
                    image_url=image_url,
                    distance=0,
                    bits=len(phash) * 4,
                )
            )
        _ph_ints = ph_ints
        _rows = meta
        _row_count = count
        _loaded_at = time.monotonic()
        logger.info("catalog hash index loaded: %d rows", len(_ph_ints))


async def find_nearest(
    db: AsyncSession,
    phash: str,
    *,
    tcg: str | None = None,
    max_distance: int | None = None,
) -> CatalogHashHit | None:
    """Nearest catalog card to ``phash`` by Hamming distance, or ``None``.

    ``tcg`` (when the user picked a game) scopes the scan to that game's rows.
    ``max_distance`` defaults to ``settings.phash_max_distance``.
    """
    settings = get_settings()
    if not settings.phash_enabled or not phash:
        return None
    query_int = _hex_to_int(phash)
    if query_int is None:
        return None
    try:
        await _refresh_if_stale(db)
    except Exception:
        logger.exception("catalog hash index refresh failed")
        return None
    if not _ph_ints:
        return None

    threshold = settings.phash_max_distance if max_distance is None else max_distance
    tcg_norm = tcg.lower() if tcg and tcg.lower() != "all" else None

    best_i = -1
    best_dist = 1 << 30
    ph_ints = _ph_ints
    rows = _rows
    for i, cand in enumerate(ph_ints):
        # Scope to the selected game FIRST so a nearer out-of-game row can't
        # crowd out the correct in-game match.
        if tcg_norm is not None and rows[i].tcg.lower() != tcg_norm:
            continue
        # int XOR + popcount — the whole hot loop.
        dist = (query_int ^ cand).bit_count()
        if dist < best_dist:
            best_dist = dist
            best_i = i
            if dist == 0:
                break

    if best_i < 0 or best_dist > threshold:
        return None
    hit = rows[best_i]
    return CatalogHashHit(
        upstream_id=hit.upstream_id,
        tcg=hit.tcg,
        name=hit.name,
        set_name=hit.set_name,
        number=hit.number,
        image_url=hit.image_url,
        distance=best_dist,
        bits=hit.bits,
    )


def _reset_for_tests() -> None:
    """Drop the cache so a test's freshly-seeded rows are picked up."""
    global _loaded_at, _row_count, _ph_ints, _rows
    _loaded_at = 0.0
    _row_count = -1
    _ph_ints = []
    _rows = []


__all__ = ["CatalogHashHit", "find_nearest"]
