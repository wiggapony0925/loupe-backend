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
from typing import Any

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
_dh_ints: list[int | None] = []  # dHash cross-check for margin-accepted hits
_rows: list[CatalogHashHit] = []  # distance filled per-query via _clone_with


def _hex_to_int(h: str) -> int | None:
    try:
        return int(h, 16)
    except (ValueError, TypeError):
        return None


async def _refresh_if_stale(db: AsyncSession) -> None:
    global _loaded_at, _row_count, _ph_ints, _dh_ints, _rows
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
                    CatalogImageHash.dhash,
                    CatalogImageHash.phash_alt,
                    CatalogImageHash.dhash_alt,
                )
            )
        ).all()
        ph_ints: list[int] = []
        dh_ints: list[int | None] = []
        meta: list[CatalogHashHit] = []
        for (
            upstream_id,
            tcg,
            name,
            set_name,
            number,
            image_url,
            phash,
            dhash,
            phash_alt,
            dhash_alt,
        ) in rows:
            hit = CatalogHashHit(
                upstream_id=upstream_id,
                tcg=tcg,
                name=name,
                set_name=set_name,
                number=number,
                image_url=image_url,
                distance=0,
                bits=len(phash) * 4,
            )
            # A card contributes one entry per art scan it has — the upstream's
            # small and _hires scans can differ by 40+ bits, so both must be
            # matchable. Entries share the same meta row.
            for ph, dh in ((phash, dhash), (phash_alt, dhash_alt)):
                val = _hex_to_int(ph) if ph else None
                if val is None:
                    continue
                ph_ints.append(val)
                dh_ints.append(_hex_to_int(dh) if dh else None)
                meta.append(hit)
        _ph_ints = ph_ints
        _dh_ints = dh_ints
        _rows = meta
        _row_count = count
        _loaded_at = time.monotonic()
        logger.info("catalog hash index loaded: %d entries", len(_ph_ints))


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


@dataclass(frozen=True)
class BestMatch:
    """Best index hit across every query variant, with the safety numbers."""

    hit: CatalogHashHit
    #: Best pHash Hamming distance over all variants.
    distance: int
    #: Distance of the closest DIFFERENT card — the margin denominator. A big
    #: gap means the best hit is unambiguous even at a loose absolute distance.
    runner_up_distance: int
    #: dHash distance of the winning variant against the row (None = unknown).
    dhash_distance: int | None


async def find_best(
    db: AsyncSession,
    fingerprints: list[tuple[str, str | None]],
    *,
    tcg: str | None = None,
) -> BestMatch | None:
    """Best catalog card across MULTIPLE query fingerprints (frame + camera-
    correction variants), with the runner-up distance for margin acceptance.

    ``fingerprints`` is ``[(phash, dhash), …]``. The caller decides acceptance
    (strict distance / margin rule) — this just reports the numbers honestly.
    """
    settings = get_settings()
    if not settings.phash_enabled or not fingerprints:
        return None
    try:
        await _refresh_if_stale(db)
    except Exception:
        logger.exception("catalog hash index refresh failed")
        return None
    if not _ph_ints:
        return None

    tcg_norm = tcg.lower() if tcg and tcg.lower() != "all" else None
    ph_ints = _ph_ints
    rows = _rows

    best_i = -1
    best_dist = 1 << 30
    best_q_dhash: int | None = None
    runner_up = 1 << 30
    for phash, dhash in fingerprints:
        q = _hex_to_int(phash)
        if q is None:
            continue
        q_dh = _hex_to_int(dhash) if dhash else None
        for i, cand in enumerate(ph_ints):
            if tcg_norm is not None and rows[i].tcg.lower() != tcg_norm:
                continue
            dist = (q ^ cand).bit_count()
            # The margin runner-up is the nearest DIFFERENTLY-NAMED card:
            # reprints share the exact artwork, so a same-name twin sitting a
            # few bits away is expected and must not destroy decisiveness
            # (and a card's own alternate-art entry must never count).
            if dist < best_dist:
                if best_i >= 0 and rows[best_i].name.lower() != rows[i].name.lower():
                    runner_up = min(runner_up, best_dist)
                best_dist = dist
                best_i = i
                best_q_dhash = q_dh
            elif (
                best_i >= 0
                and rows[i].name.lower() != rows[best_i].name.lower()
                and dist < runner_up
            ):
                runner_up = dist

    if best_i < 0:
        return None
    hit = rows[best_i]
    dh_dist: int | None = None
    if best_q_dhash is not None and _dh_ints[best_i] is not None:
        dh_dist = (best_q_dhash ^ _dh_ints[best_i]).bit_count()  # type: ignore[operator]
    return BestMatch(
        hit=CatalogHashHit(
            upstream_id=hit.upstream_id,
            tcg=hit.tcg,
            name=hit.name,
            set_name=hit.set_name,
            number=hit.number,
            image_url=hit.image_url,
            distance=best_dist,
            bits=hit.bits,
        ),
        distance=best_dist,
        runner_up_distance=runner_up,
        dhash_distance=dh_dist,
    )


async def search_text(
    db: AsyncSession,
    *,
    tcg: str | None,
    titles: list[str],
    number: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """LOCAL candidate search over the denormalized index — the fast path for
    the OCR pipeline. Replaces a multi-second (and flaky) upstream catalog
    search with an in-memory scan of names we already hold for every card.

    Returns flat candidate dicts (``id``/``name``/``set_name``/``number``/
    ``image_url``/``tcg``) compatible with the identify scorer. Empty when the
    index is cold/unavailable — callers fall back to the upstream search.
    """
    settings = get_settings()
    if not settings.phash_enabled or not titles:
        return []
    try:
        await _refresh_if_stale(db)
    except Exception:
        logger.exception("catalog hash index refresh failed")
        return []
    if not _rows:
        return []

    # Late import — card_search_service imports are heavy; relevance_score is
    # a pure function shared with the live search ranking.
    from app.services.catalog.card_search_service import relevance_score

    tcg_norm = tcg.lower() if tcg and tcg.lower() != "all" else None
    num_norm = (number or "").strip().lstrip("0").lower() or None

    scored: list[tuple[float, CatalogHashHit]] = []
    for row in _rows:
        if tcg_norm is not None and row.tcg.lower() != tcg_norm:
            continue
        name_score = max(relevance_score(row.name, t) for t in titles)
        if name_score < 0.45:
            continue
        score = name_score
        if num_norm is not None:
            row_num = (row.number or "").strip().lstrip("0").lower()
            if row_num == num_norm:
                score += 0.5  # exact collector number = the printing in hand
            elif row_num:
                score -= 0.1
        scored.append((score, row))

    scored.sort(key=lambda s: -s[0])
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for _, r in scored:
        # Cards contribute one meta row per art entry — dedupe by id.
        if r.upstream_id in seen_ids:
            continue
        seen_ids.add(r.upstream_id)
        out.append(
            {
                "id": r.upstream_id,
                "name": r.name,
                "tcg": r.tcg,
                "set_name": r.set_name,
                "number": r.number,
                "image_url": r.image_url,
            }
        )
        if len(out) >= limit:
            break
    return out


async def warm(db: AsyncSession) -> None:
    """Load the index eagerly (startup) so the first scan never pays it."""
    try:
        await _refresh_if_stale(db)
    except Exception:
        logger.exception("catalog hash index warm failed")


def _reset_for_tests() -> None:
    """Drop the cache so a test's freshly-seeded rows are picked up."""
    global _loaded_at, _row_count, _ph_ints, _dh_ints, _rows
    _loaded_at = 0.0
    _row_count = -1
    _ph_ints = []
    _dh_ints = []
    _rows = []


__all__ = [
    "BestMatch",
    "CatalogHashHit",
    "find_best",
    "find_nearest",
    "search_text",
    "warm",
]
