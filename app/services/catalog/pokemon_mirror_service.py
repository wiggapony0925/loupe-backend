"""Pokémon catalog mirror — sync, and millisecond reads for browse/search/detail.

The mirror keeps the COMPLETE Pokémon catalog (every set, every printing) in
Postgres, in the exact JSON shape api.pokemontcg.io returns, so the existing
``_from_pokemon`` converter renders mirror rows identically to live responses.

Identity (names, sets, numbers, art) syncs from the upstream's own bulk data
repo — ``PokemonTCG/pokemon-tcg-data`` on GitHub — which is the source the API
serves from: complete, current, keyless, and served by GitHub's CDN instead of
the flaky API host. The dump carries **no price blocks**; embedded
tcgplayer/cardmarket prices hydrate per set from the live API in background
refreshes (see :func:`maybe_refresh_set_prices`) and are preserved across
re-syncs.

Read helpers return raw payload dicts (not unified cards) so this module never
imports :mod:`card_search_service` — callers convert with ``_from_pokemon``.
Every read helper returns ``None`` when the mirror isn't populated yet, which
callers treat as "fall back to the live proxy".
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select

from app.integrations._http import pokemon_tcg
from app.integrations._http._resilient import request_json
from app.utils.logger import get_logger

logger = get_logger("services.pokemon_mirror")

DUMP_BASE = "https://raw.githubusercontent.com/PokemonTCG/pokemon-tcg-data/master"
SOURCE = "pokemontcg"
TCG = "pokemon"

#: Mirror counts below this mean "not really synced" — fall back to live.
_READY_MIN_CARDS = 1_000
#: How long a set's embedded prices are considered fresh.
_PRICE_FRESH = timedelta(hours=24)
#: Concurrent set-file fetches during a dump sync.
_SYNC_CONCURRENCY = 6

_NUM_PREFIX_RE = re.compile(r"^(\d+)")

#: Keep refs to fire-and-forget refresh tasks (asyncio holds only weak refs).
_bg_tasks: set[asyncio.Task[Any]] = set()
#: In-process single-flight guard for per-set price refreshes.
_price_refresh_inflight: set[str] = set()

#: (checked_at_monotonic, ready) — readiness is polled at most once a minute.
_ready_cache: tuple[float, bool] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _sessionmaker():
    from app.db import get_sessionmaker

    return get_sessionmaker()


def reset_ready_cache() -> None:
    """Test helper — force the next :func:`mirror_ready` to re-query."""
    global _ready_cache
    _ready_cache = None


# --------------------------------------------------------------------- shape


def _bare_number(number: str | None) -> str | None:
    """``"058/102"`` → ``"58"`` (same reduction the identify pipeline uses)."""
    if not number:
        return None
    left = str(number).split("/", 1)[0].strip().lstrip("0")
    return left if left.isdigit() else None


def _number_int(number: str | None) -> int | None:
    if not number:
        return None
    m = _NUM_PREFIX_RE.match(str(number).strip())
    return int(m.group(1)) if m else None


def _sort_price(card: dict[str, Any]) -> float | None:
    """Best embedded market price — mirrors ``_pokemon_pricing``'s variant
    priority so price sorts agree with the price the tile displays."""
    prices = (card.get("tcgplayer") or {}).get("prices") or {}
    for variant in (
        "holofoil",
        "reverseHolofoil",
        "1stEditionHolofoil",
        "normal",
        "1stEdition",
        "unlimited",
        "unlimitedHolofoil",
    ):
        v = prices.get(variant)
        if isinstance(v, dict):
            for key in ("market", "mid"):
                try:
                    val = float(v.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if val > 0:
                    return round(val, 2)
    cm = (card.get("cardmarket") or {}).get("prices") or {}
    for key in ("averageSellPrice", "trendPrice"):
        try:
            val = float(cm.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return round(val, 2)
    return None


def _columns_from_payload(card: dict[str, Any]) -> dict[str, Any]:
    """Extract the searchable/sortable columns for one API-shaped card."""
    set_obj = card.get("set") or {}
    upstream_id = str(card.get("id") or "")
    name = str(card.get("name") or "")
    number = card.get("number")
    return {
        "id": f"{SOURCE}:{upstream_id}",
        "source": SOURCE,
        "tcg": TCG,
        "upstream_id": upstream_id,
        "set_id": str(set_obj.get("id") or ""),
        "set_name": set_obj.get("name"),
        "name": name[:200],
        "name_lower": name.lower()[:200],
        "number": str(number)[:40] if number is not None else None,
        "bare_number": _bare_number(number),
        "number_int": _number_int(number),
        "rarity": (card.get("rarity") or None),
        "release_date": set_obj.get("releaseDate"),
        "sort_price": _sort_price(card),
        "payload": card,
        "synced_at": _now(),
    }


# ---------------------------------------------------------------------- sync


async def _fetch_dump(path: str) -> Any:
    """GET a JSON file from the bulk-data repo (GitHub raw CDN)."""
    return await request_json(
        integration="pokemontcg_dump",
        method="GET",
        url=f"{DUMP_BASE}/{path}",
        headers={"Accept": "application/json"},
        timeout_s=30.0,
    )


async def sync_pokemon_from_dump(
    *, force: bool = False, max_sets: int | None = None
) -> dict[str, Any]:
    """Sync sets + cards from the bulk-data dump into the mirror.

    Idempotent and resumable: a set whose mirror row count already matches the
    dump total is skipped unless ``force``. Each set commits independently, so
    an interrupted sync picks up where it left off. Existing embedded price
    blocks are preserved (the dump has none).

    Returns counters for the admin surface.
    """
    from app.models.catalog_mirror import CatalogMirrorSet

    dump_sets = await _fetch_dump("sets/en.json")
    if not isinstance(dump_sets, list) or not dump_sets:
        raise RuntimeError("pokemon dump sets/en.json unavailable or empty")

    stats = {
        "sets_total": len(dump_sets),
        "sets_synced": 0,
        "sets_skipped": 0,
        "cards_synced": 0,
        "errors": 0,
    }

    maker = _sessionmaker()
    async with maker() as session:
        existing_counts = {
            row[0]: (row[1], row[2])
            for row in (
                await session.execute(
                    select(
                        CatalogMirrorSet.id,
                        CatalogMirrorSet.card_count,
                        CatalogMirrorSet.total,
                    ).where(CatalogMirrorSet.source == SOURCE)
                )
            ).all()
        }

    # Newest sets first so a partial/interrupted sync covers the cards
    # users actually look for before the 1999 backlist.
    ordered = sorted(
        [s for s in dump_sets if isinstance(s, dict) and s.get("id")],
        key=lambda s: str(s.get("releaseDate") or ""),
        reverse=True,
    )
    pending: list[dict[str, Any]] = []
    for s in ordered:
        have = existing_counts.get(str(s["id"]), (0, None))[0]
        if not force and have >= max(int(s.get("total") or 0), 1):
            # Complete already — refresh the set row's metadata only (totals
            # change when secret rares get added) and skip the card file.
            await _upsert_set_row(s, card_count=have)
            stats["sets_skipped"] += 1
            continue
        pending.append(s)
    if max_sets is not None:
        pending = pending[:max_sets]

    sem = asyncio.Semaphore(_SYNC_CONCURRENCY)

    async def _fetch_set_cards(set_obj: dict[str, Any]) -> tuple[dict, Any]:
        async with sem:
            try:
                return set_obj, await _fetch_dump(f"cards/en/{set_obj['id']}.json")
            except Exception as exc:
                logger.warning("dump fetch failed set=%s: %s", set_obj["id"], exc)
                return set_obj, None

    for coro in asyncio.as_completed([_fetch_set_cards(s) for s in pending]):
        set_obj, cards = await coro
        set_id = str(set_obj["id"])
        if cards is None or not isinstance(cards, list):
            stats["errors"] += 1
            continue
        try:
            n = await _upsert_set_cards(set_obj, cards)
            stats["sets_synced"] += 1
            stats["cards_synced"] += n
        except Exception as exc:
            logger.warning("mirror upsert failed set=%s: %s", set_id, exc)
            stats["errors"] += 1

    reset_ready_cache()
    logger.info("pokemon mirror sync: %s", stats)
    return stats


async def _upsert_set_row(set_obj: dict[str, Any], *, card_count: int | None) -> None:
    from app.models.catalog_mirror import CatalogMirrorSet

    set_id = str(set_obj["id"])
    maker = _sessionmaker()
    async with maker() as session:
        row = await session.get(CatalogMirrorSet, set_id)
        if row is None:
            row = CatalogMirrorSet(id=set_id, source=SOURCE, tcg=TCG, name="")
            session.add(row)
        row.name = str(set_obj.get("name") or row.name or "Unknown")[:200]
        row.series = set_obj.get("series") or None
        row.release_date = set_obj.get("releaseDate")
        row.printed_total = set_obj.get("printedTotal")
        row.total = set_obj.get("total")
        row.payload = set_obj
        if card_count is not None:
            row.card_count = card_count
        row.synced_at = _now()
        await session.commit()


async def _upsert_set_cards(set_obj: dict[str, Any], cards: list[Any]) -> int:
    """Write one set's cards. Preserves embedded price blocks on existing rows
    (the dump carries none). Commits once per set — resumable."""
    from app.models.catalog_mirror import CatalogMirrorCard

    set_id = str(set_obj["id"])
    maker = _sessionmaker()
    async with maker() as session:
        existing = {
            r.id: r
            for r in (
                await session.execute(
                    select(CatalogMirrorCard).where(
                        CatalogMirrorCard.set_id == set_id,
                        CatalogMirrorCard.source == SOURCE,
                    )
                )
            )
            .scalars()
            .all()
        }
        count = 0
        for card in cards:
            if not isinstance(card, dict) or not card.get("id"):
                continue
            payload = dict(card)
            payload["set"] = set_obj
            row_id = f"{SOURCE}:{payload['id']}"
            prev = existing.get(row_id)
            if prev is not None:
                old = prev.payload or {}
                for block in ("tcgplayer", "cardmarket"):
                    if block not in payload and old.get(block):
                        payload[block] = old[block]
            cols = _columns_from_payload(payload)
            if prev is not None:
                for k, v in cols.items():
                    if k != "id":
                        setattr(prev, k, v)
            else:
                session.add(CatalogMirrorCard(**cols))
            count += 1
        await session.commit()
    await _upsert_set_row(set_obj, card_count=count)
    return count


# ------------------------------------------------------------- price refresh


async def refresh_set_prices(set_id: str) -> int:
    """Refresh embedded tcgplayer/cardmarket blocks for one set from the live
    API. Never raises; returns how many cards got updated prices."""
    from app.models.catalog_mirror import CatalogMirrorCard, CatalogMirrorSet

    updated = 0
    try:
        page = 1
        merged: dict[str, dict[str, Any]] = {}
        while True:
            raw = await pokemon_tcg.search_cards(
                f"set.id:{set_id}",
                page=page,
                page_size=250,
                timeout_s=8.0,
            )
            data = raw.get("data") or []
            for c in data:
                if isinstance(c, dict) and c.get("id"):
                    merged[str(c["id"])] = c
            total = int(raw.get("totalCount") or 0)
            if not data or page * 250 >= total:
                break
            page += 1
        if not merged:
            return 0

        maker = _sessionmaker()
        async with maker() as session:
            rows = (
                (
                    await session.execute(
                        select(CatalogMirrorCard).where(
                            CatalogMirrorCard.set_id == set_id,
                            CatalogMirrorCard.source == SOURCE,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                live = merged.get(row.upstream_id)
                if live is None:
                    continue
                payload = dict(row.payload or {})
                changed = False
                for block in ("tcgplayer", "cardmarket"):
                    if live.get(block):
                        payload[block] = live[block]
                        changed = True
                if not changed:
                    continue
                row.payload = payload
                row.sort_price = _sort_price(payload)
                row.synced_at = _now()
                updated += 1
            set_row = await session.get(CatalogMirrorSet, set_id)
            if set_row is not None:
                set_row.prices_synced_at = _now()
            await session.commit()
    except Exception as exc:
        logger.info("price refresh failed set=%s: %s", set_id, exc)
    return updated


def maybe_refresh_set_prices(set_ids: list[str]) -> None:
    """Fire-and-forget: refresh prices for any of *set_ids* whose embedded
    prices are stale (> 24h) or missing. Zero cost on the request path —
    staleness is checked inside the background task. In-process single-flight
    per set."""
    from app.config import get_settings

    # The suite runs offline (see tests/conftest.py) — a background price
    # refresh spawned by a mirror read would hit the live API mid-test.
    if get_settings().app_env == "test":
        return
    ids = [s for s in dict.fromkeys(set_ids) if s and s not in _price_refresh_inflight]
    if not ids:
        return

    async def _runner(batch: list[str]) -> None:
        from app.models.catalog_mirror import CatalogMirrorSet

        try:
            maker = _sessionmaker()
            async with maker() as session:
                rows = (
                    await session.execute(
                        select(
                            CatalogMirrorSet.id, CatalogMirrorSet.prices_synced_at
                        ).where(CatalogMirrorSet.id.in_(batch))
                    )
                ).all()
            cutoff = _now() - _PRICE_FRESH
            for sid, synced in rows:
                if synced is not None:
                    synced_utc = (
                        synced
                        if synced.tzinfo is not None
                        else synced.replace(tzinfo=UTC)
                    )
                    if synced_utc > cutoff:
                        continue
                await refresh_set_prices(sid)
        except Exception as exc:  # pragma: no cover - background best effort
            logger.debug("background price refresh failed: %s", exc)
        finally:
            for sid in batch:
                _price_refresh_inflight.discard(sid)

    _price_refresh_inflight.update(ids)
    try:
        task = asyncio.create_task(_runner(ids))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    except RuntimeError:  # no running loop (sync tests) — skip quietly
        for sid in ids:
            _price_refresh_inflight.discard(sid)


# --------------------------------------------------------------------- reads


async def mirror_ready() -> bool:
    """True once the mirror holds a real catalog (cached for 60s)."""
    global _ready_cache
    now = time.monotonic()
    if _ready_cache is not None and now - _ready_cache[0] < 60:
        return _ready_cache[1]
    try:
        from app.models.catalog_mirror import CatalogMirrorCard

        maker = _sessionmaker()
        async with maker() as session:
            n = (
                await session.execute(
                    select(func.count())
                    .select_from(CatalogMirrorCard)
                    .where(CatalogMirrorCard.tcg == TCG)
                )
            ).scalar_one()
        ready = int(n) >= _READY_MIN_CARDS
    except Exception as exc:  # pragma: no cover - e.g. table not migrated yet
        logger.debug("mirror_ready check failed: %s", exc)
        ready = False
    _ready_cache = (now, ready)
    return ready


def _null_last(col):
    return case((col.is_(None), 1), else_=0)


async def browse_pokemon(
    page: int, page_size: int, sort: str, set_id: str | None = None
) -> dict[str, Any] | None:
    """One browse page from the mirror — ``None`` when the mirror isn't ready.

    Returns ``{"payloads": [...], "total": int}``; also kicks a background
    price refresh for the sets visible on the page."""
    if not await mirror_ready():
        return None
    from app.models.catalog_mirror import CatalogMirrorCard

    C = CatalogMirrorCard
    where = [C.tcg == TCG]
    if set_id:
        where.append(C.set_id == set_id)

    order = {
        "name": (C.name_lower, C.id),
        "newest": (
            _null_last(C.release_date),
            C.release_date.desc(),
            C.set_id,
            _null_last(C.number_int),
            C.number_int,
        ),
        "price_asc": (_null_last(C.sort_price), C.sort_price, C.id),
        "price_desc": (_null_last(C.sort_price), C.sort_price.desc(), C.id),
    }.get(sort) or (C.name_lower, C.id)

    maker = _sessionmaker()
    async with maker() as session:
        total = (
            await session.execute(select(func.count()).select_from(C).where(*where))
        ).scalar_one()
        rows = (
            await session.execute(
                select(C.payload, C.set_id)
                .where(*where)
                .order_by(*order)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    maybe_refresh_set_prices([r[1] for r in rows])
    return {"payloads": [r[0] for r in rows], "total": int(total)}


def _token_filters(C, q: str) -> list[Any]:
    tokens = [t for t in re.sub(r"[^a-z0-9\- ]+", " ", q.lower()).split() if t]
    return [C.name_lower.like(f"%{tok}%") for tok in tokens]


async def search_pokemon(
    q: str, *, page: int = 1, page_size: int = 60
) -> dict[str, Any] | None:
    """Substring search over the full mirror with real pagination.

    All name tokens must match (AND); when that yields nothing, degrade to
    ANY-token (OR) like the live "relaxed" query. Newest printings first —
    the same ordering the live paged search used. ``None`` = mirror not ready.
    """
    if not await mirror_ready():
        return None
    from app.models.catalog_mirror import CatalogMirrorCard

    C = CatalogMirrorCard
    filters = _token_filters(C, q)
    if not filters:
        return {"payloads": [], "total": 0}

    order = (
        _null_last(C.release_date),
        C.release_date.desc(),
        C.set_id,
        _null_last(C.number_int),
        C.number_int,
    )
    maker = _sessionmaker()
    async with maker() as session:
        for clause in (
            [C.tcg == TCG, *filters],
            [C.tcg == TCG, or_(*filters)] if len(filters) > 1 else None,
        ):
            if clause is None:
                continue
            total = (
                await session.execute(
                    select(func.count()).select_from(C).where(*clause)
                )
            ).scalar_one()
            if not total:
                continue
            rows = (
                await session.execute(
                    select(C.payload, C.set_id)
                    .where(*clause)
                    .order_by(*order)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
            maybe_refresh_set_prices([r[1] for r in rows])
            return {"payloads": [r[0] for r in rows], "total": int(total)}
    return {"payloads": [], "total": 0}


async def precise_pokemon(
    name: str, bare_number: str, limit: int = 10
) -> list[dict[str, Any]] | None:
    """Identify-pipeline lookup: pin the collector number, prefer name-token
    matches, and fall back to number-only (OCR-garbled names). ``None`` =
    mirror not ready; ``[]`` = genuinely nothing with that number."""
    if not await mirror_ready():
        return None
    from app.models.catalog_mirror import CatalogMirrorCard

    C = CatalogMirrorCard
    filters = _token_filters(C, name)
    maker = _sessionmaker()
    async with maker() as session:
        if filters:
            rows = (
                await session.execute(
                    select(C.payload)
                    .where(C.tcg == TCG, C.bare_number == bare_number, *filters)
                    .limit(limit)
                )
            ).all()
            if rows:
                return [r[0] for r in rows]
        rows = (
            await session.execute(
                select(C.payload)
                .where(C.tcg == TCG, C.bare_number == bare_number)
                .limit(max(limit, 30))
            )
        ).all()
    return [r[0] for r in rows]


async def get_pokemon_by_id(upstream_id: str) -> dict[str, Any] | None:
    """Single card payload by provider-local id (``me4-1``); also kicks a
    background price refresh for its set when stale. Misses return ``None``
    (callers then try the live API)."""
    from app.models.catalog_mirror import CatalogMirrorCard

    try:
        maker = _sessionmaker()
        async with maker() as session:
            row = (
                await session.execute(
                    select(CatalogMirrorCard.payload, CatalogMirrorCard.set_id).where(
                        CatalogMirrorCard.id == f"{SOURCE}:{upstream_id}"
                    )
                )
            ).first()
    except Exception as exc:  # pragma: no cover - table missing pre-migration
        logger.debug("mirror get failed id=%s: %s", upstream_id, exc)
        return None
    if row is None:
        return None
    maybe_refresh_set_prices([row[1]])
    return row[0]


async def upsert_card_payload(card: dict[str, Any]) -> None:
    """Opportunistically store one live API card (full shape, with ``set``).

    Called when a card was fetched live because the mirror missed it — the
    next view is a mirror hit. Best effort."""
    if not isinstance(card, dict) or not card.get("id") or not card.get("set"):
        return
    try:
        from app.models.catalog_mirror import CatalogMirrorCard

        cols = _columns_from_payload(card)
        maker = _sessionmaker()
        async with maker() as session:
            row = await session.get(CatalogMirrorCard, cols["id"])
            if row is None:
                session.add(CatalogMirrorCard(**cols))
            else:
                for k, v in cols.items():
                    if k != "id":
                        setattr(row, k, v)
            await session.commit()
    except Exception as exc:  # pragma: no cover
        logger.debug("mirror opportunistic upsert failed: %s", exc)


async def list_pokemon_sets() -> list[dict[str, Any]] | None:
    """All mirrored sets (API payload shape), newest first. ``None`` when the
    mirror isn't ready."""
    if not await mirror_ready():
        return None
    from app.models.catalog_mirror import CatalogMirrorSet

    S = CatalogMirrorSet
    maker = _sessionmaker()
    async with maker() as session:
        rows = (
            await session.execute(
                select(S.payload)
                .where(S.source == SOURCE)
                .order_by(_null_last(S.release_date), S.release_date.desc())
            )
        ).all()
    return [r[0] or {} for r in rows]


async def mirror_status() -> dict[str, Any]:
    """Counts for the admin surface."""
    from app.models.catalog_mirror import CatalogMirrorCard, CatalogMirrorSet

    maker = _sessionmaker()
    async with maker() as session:
        cards = (
            await session.execute(
                select(func.count())
                .select_from(CatalogMirrorCard)
                .where(CatalogMirrorCard.tcg == TCG)
            )
        ).scalar_one()
        priced = (
            await session.execute(
                select(func.count())
                .select_from(CatalogMirrorCard)
                .where(
                    CatalogMirrorCard.tcg == TCG,
                    CatalogMirrorCard.sort_price.is_not(None),
                )
            )
        ).scalar_one()
        sets = (
            await session.execute(
                select(func.count())
                .select_from(CatalogMirrorSet)
                .where(CatalogMirrorSet.source == SOURCE)
            )
        ).scalar_one()
        stale_prices = (
            await session.execute(
                select(func.count())
                .select_from(CatalogMirrorSet)
                .where(
                    CatalogMirrorSet.source == SOURCE,
                    or_(
                        CatalogMirrorSet.prices_synced_at.is_(None),
                        CatalogMirrorSet.prices_synced_at < _now() - _PRICE_FRESH,
                    ),
                )
            )
        ).scalar_one()
    return {
        "ready": int(cards) >= _READY_MIN_CARDS,
        "cards": int(cards),
        "cards_priced": int(priced),
        "sets": int(sets),
        "sets_with_stale_prices": int(stale_prices),
    }


async def stale_price_set_ids(limit: int = 20) -> list[str]:
    """Oldest-stale sets, for the admin price-refresh walker."""
    from app.models.catalog_mirror import CatalogMirrorSet

    S = CatalogMirrorSet
    maker = _sessionmaker()
    async with maker() as session:
        rows = (
            await session.execute(
                select(S.id)
                .where(
                    S.source == SOURCE,
                    or_(
                        S.prices_synced_at.is_(None),
                        S.prices_synced_at < _now() - _PRICE_FRESH,
                    ),
                )
                .order_by(_null_last(S.prices_synced_at), S.prices_synced_at)
                .limit(limit)
            )
        ).all()
    return [r[0] for r in rows]


__all__ = [
    "browse_pokemon",
    "get_pokemon_by_id",
    "list_pokemon_sets",
    "maybe_refresh_set_prices",
    "mirror_ready",
    "mirror_status",
    "precise_pokemon",
    "refresh_set_prices",
    "reset_ready_cache",
    "search_pokemon",
    "stale_price_set_ids",
    "sync_pokemon_from_dump",
    "upsert_card_payload",
]
