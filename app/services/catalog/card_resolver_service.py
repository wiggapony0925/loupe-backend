"""Canonical card-identity resolver.

This service is the single funnel for *"given X, what is the canonical Loupe
card?"*  X can be:

* a free-text query (name, set, number)
* a Loupe UUID (``cards.id``)
* a composite upstream id (``<source>:<external_id>``)
* a perceptual image hash (pHash)

It composes the existing pieces — :mod:`card_search_service`,
:mod:`card_catalog_service`, :mod:`fingerprint_service`, and the new
``card_external_refs`` table — into one cohesive API so that scans,
search hits, deep links, and worker backfills all share the same
identity logic.

The resolver never raises on upstream/provider failures; misses return
``None``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.card import Card, CardSet
from app.models.card_external_ref import CardExternalRef
from app.models.enums import TcgEnum
from app.models.fingerprint import Fingerprint
from app.services.catalog import (
    card_catalog_service,
    card_search_service,
    catalog_hash_index,
)
from app.utils.logger import get_logger

logger = get_logger("services.card_resolver")


@dataclass(frozen=True)
class ResolvedCard:
    """A canonical card plus the upstream payload (if available)."""

    card_id: uuid.UUID | None
    upstream_id: str | None  # ``<source>:<external_id>`` form
    unified: dict[str, Any] | None  # the rich card dict from card_search_service
    source: str  # 'local' | 'upstream' | 'fingerprint' | 'external_ref'
    confidence: float
    #: True when the art-hash match is unambiguous (strict distance, or a
    #: loose distance with a decisive margin over the runner-up + dHash
    #: agreement) — the identify pipeline may skip OCR entirely.
    decisive: bool = False


# --------------------------------------------------------------------- by id


async def resolve_by_uuid(db: AsyncSession, card_id: uuid.UUID) -> ResolvedCard | None:
    """Look up a local Card by UUID and return the unified payload."""
    row = await card_catalog_service.get_card(db, card_id)
    if row is None:
        return None
    unified = await card_search_service.get_card(str(card_id))
    upstream_id = await _preferred_external_ref(db, card_id)
    return ResolvedCard(
        card_id=card_id,
        upstream_id=upstream_id,
        unified=unified,
        source="local",
        confidence=1.0,
    )


async def resolve_by_upstream_id(
    db: AsyncSession, upstream_id: str
) -> ResolvedCard | None:
    """Look up by ``<source>:<external_id>`` and return + link to a local Card.

    If a local ``Card`` with this external ref doesn't exist yet, the upstream
    payload is still returned with ``card_id=None`` so callers can decide
    whether to materialize it locally.
    """
    if ":" not in upstream_id:
        return None
    source, _, external = upstream_id.partition(":")
    source = source.lower()

    ref = (
        await db.execute(
            select(CardExternalRef).where(
                CardExternalRef.source == source,
                CardExternalRef.external_id == external,
            )
        )
    ).scalar_one_or_none()

    unified = await card_search_service.get_card(upstream_id)
    if unified is None and ref is None:
        return None

    return ResolvedCard(
        card_id=ref.card_id if ref else None,
        upstream_id=upstream_id,
        unified=unified,
        source="external_ref" if ref else "upstream",
        confidence=float(ref.confidence) if ref and ref.confidence else 1.0,
    )


# ------------------------------------------------------------------- by text


async def resolve_by_text(
    db: AsyncSession,
    *,
    query: str,
    tcg: str | None = None,
) -> ResolvedCard | None:
    """Best-effort resolution from a free-text query (scan OCR, search bar)."""
    body = await card_search_service.search_cards(q=query, tcg=tcg or "all", limit=1)
    results = body.get("results") or []
    if not results:
        return None
    top = results[0]
    upstream_id = top.get("id")  # always composite for live-search hits

    card_id = None
    if upstream_id and ":" in upstream_id:
        source, _, external = upstream_id.partition(":")
        ref = (
            await db.execute(
                select(CardExternalRef).where(
                    CardExternalRef.source == source,
                    CardExternalRef.external_id == external,
                )
            )
        ).scalar_one_or_none()
        if ref:
            card_id = ref.card_id

    return ResolvedCard(
        card_id=card_id,
        upstream_id=upstream_id,
        unified=top,
        source="upstream",
        confidence=0.85,  # text matches are best-guess
    )


# ------------------------------------------------------------- by fingerprint


async def resolve_by_phash(
    db: AsyncSession, phash: str, *, max_distance: int = 8
) -> ResolvedCard | None:
    """Look up a card by perceptual image hash (Hamming distance ≤ max_distance).

    Walks rows whose ``phash`` shares any of the first 8 hex chars and picks
    the closest. Cheap enough for ≤50k catalog images; switch to pgvector or
    bk-tree when that no longer fits.
    """
    if not phash or len(phash) < 8:
        return None
    prefix = phash[:8]
    candidates = (
        (
            await db.execute(
                select(Fingerprint).where(Fingerprint.phash.startswith(prefix[:4]))
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return None
    best = min(candidates, key=lambda f: _hamming(phash, f.phash or "ff" * 32))
    distance = _hamming(phash, best.phash or "")
    if distance > max_distance:
        return None
    if best.graded_card_id is None:
        return None
    # Resolve from graded_card → card_id
    from app.models.grade import GradedCard

    gc = (
        await db.execute(select(GradedCard).where(GradedCard.id == best.graded_card_id))
    ).scalar_one_or_none()
    if gc is None or gc.card_id is None:
        return None
    resolved = await resolve_by_uuid(db, gc.card_id)
    if resolved is None:
        return None
    confidence = max(0.0, 1.0 - (distance / 64.0))
    return ResolvedCard(
        card_id=resolved.card_id,
        upstream_id=resolved.upstream_id,
        unified=resolved.unified,
        source="fingerprint",
        confidence=round(confidence, 2),
    )


async def resolve_catalog_by_phash(
    db: AsyncSession, phash: str, *, max_distance: int | None = None
) -> ResolvedCard | None:
    """Match a scanned frame's pHash against the *catalog* art hashes.

    Unlike :func:`resolve_by_phash` (which only sees hashes of cards users
    have already submitted, via the ``fingerprints`` table) this scans the
    ``cards.image_phash`` column the image-index worker backfills for every
    catalog card. That makes a brand-new scan identifiable purely from its
    artwork even when OCR is weak or absent.

    Picks the catalog card with the smallest Hamming distance and accepts
    it only when that distance is within ``phash_max_distance``. Loads just
    ``(id, image_phash)`` for the distance pass to keep the scan cheap;
    swap to a BK-tree / pgvector index when the catalog outgrows a linear
    scan.
    """
    settings = get_settings()
    if not settings.phash_enabled:
        return None
    if not phash or len(phash) < 8:
        return None
    threshold = settings.phash_max_distance if max_distance is None else max_distance

    bits = len(phash) * 4

    # (a) Local materialized cards (the small set with a backfilled hash).
    local_best_id, local_best_distance = await _best_local_card_phash(db, phash)

    # (b) The full-catalog art-hash index (every card, not just materialized
    # ones) — this is what makes a brand-new scan of any card matchable.
    index_hit = await catalog_hash_index.find_nearest(db, phash, max_distance=threshold)

    # Pick whichever signal is the closer match. The local table wins ties
    # (it may carry a resolved local card_id / richer payload).
    use_index = index_hit is not None and (
        local_best_id is None or index_hit.distance < local_best_distance
    )

    if use_index and index_hit is not None:
        # Build the candidate from the row's DENORMALIZED identity — never an
        # upstream fetch here. (A live get_card against pokemontcg.io added
        # 4-24s to the "fast" path; the index carries everything a scan
        # candidate needs precisely so this stays milliseconds.)
        return ResolvedCard(
            card_id=None,
            upstream_id=index_hit.upstream_id,
            unified=_unified_from_hit(index_hit),
            source="fingerprint",
            confidence=index_hit.confidence,
            decisive=index_hit.distance <= get_settings().phash_fast_path_max_distance,
        )

    if local_best_id is None or local_best_distance > threshold:
        return None
    resolved = await resolve_by_uuid(db, local_best_id)
    if resolved is None:
        return None
    confidence = max(0.0, 1.0 - (local_best_distance / bits))
    return ResolvedCard(
        card_id=resolved.card_id,
        upstream_id=resolved.upstream_id,
        unified=resolved.unified,
        source="fingerprint",
        confidence=round(confidence, 3),
        decisive=local_best_distance <= get_settings().phash_fast_path_max_distance,
    )


async def _best_local_card_phash(
    db: AsyncSession, phash: str
) -> tuple[uuid.UUID | None, int]:
    """Closest locally-materialized Card by ``image_phash``. Returns
    ``(card_id, distance)``; ``(None, bits+1)`` when nothing is hashed."""
    bits = len(phash) * 4
    rows = (
        await db.execute(
            select(Card.id, Card.image_phash).where(Card.image_phash.is_not(None))
        )
    ).all()
    best_id: uuid.UUID | None = None
    best_distance = bits + 1  # one worse than the theoretical max
    for card_id, cand_hash in rows:
        if not cand_hash or len(cand_hash) != len(phash):
            continue
        distance = _hamming(phash, cand_hash)
        if distance < best_distance:
            best_distance = distance
            best_id = card_id
    return best_id, best_distance


def _unified_from_hit(hit: catalog_hash_index.CatalogHashHit) -> dict[str, Any]:
    """Flat candidate dict from an index row — the keys the identify scorer
    and ``_to_candidate`` read. Zero upstream calls by construction."""
    return {
        "id": hit.upstream_id,
        "name": hit.name,
        "tcg": hit.tcg,
        "set_name": hit.set_name,
        "number": hit.number,
        "image_url": hit.image_url,
    }


async def resolve_catalog_best(
    db: AsyncSession,
    fingerprints: list[tuple[str, str | None]],
    *,
    tcg: str | None = None,
) -> ResolvedCard | None:
    """Match a frame + its camera-correction variants against the catalog art
    index, with margin-based acceptance.

    Acceptance (tuned for a 256-bit pHash over a ~130k-card index):

    * ``distance <= phash_fast_path_max_distance`` — near-exact art. Decisive.
    * ``distance <= phash_margin_accept_distance`` AND the runner-up (a
      DIFFERENT card) is at least ``phash_margin_min_gap`` bits further AND
      dHash agrees when known — a hand-tilted / loosely-framed frame whose
      best match stands alone. Decisive.
    * ``distance <= phash_max_distance`` — a plausible but not decisive match;
      returned as a normal (non-decisive) candidate for the ranking pipeline.
    """
    settings = get_settings()
    if not settings.phash_enabled or not fingerprints:
        return None
    best = await catalog_hash_index.find_best(db, fingerprints, tcg=tcg)

    # Locally-materialized cards (e.g. custom/imported) may exist only in
    # ``Card.image_phash`` — check them with the base frame hash and prefer
    # the closer signal, mirroring resolve_catalog_by_phash.
    base_phash = fingerprints[0][0]
    local_id, local_distance = await _best_local_card_phash(db, base_phash)
    if (
        local_id is not None
        and (best is None or local_distance < best.distance)
        and local_distance <= settings.phash_max_distance
    ):
        resolved = await resolve_by_uuid(db, local_id)
        if resolved is not None:
            bits = len(base_phash) * 4
            return ResolvedCard(
                card_id=resolved.card_id,
                upstream_id=resolved.upstream_id,
                unified=resolved.unified,
                source="fingerprint",
                confidence=round(max(0.0, 1.0 - (local_distance / bits)), 3),
                decisive=local_distance <= settings.phash_fast_path_max_distance,
            )

    if best is None:
        return None

    margin = best.runner_up_distance - best.distance
    dhash_ok = (
        best.dhash_distance is None
        or best.dhash_distance <= settings.phash_dhash_max_distance
    )
    strict = best.distance <= settings.phash_fast_path_max_distance
    margin_hit = (
        best.distance <= settings.phash_margin_accept_distance
        and margin >= settings.phash_margin_min_gap
        and dhash_ok
    )
    if not strict and not margin_hit and best.distance > settings.phash_max_distance:
        return None

    confidence = max(0.0, 1.0 - (best.distance / max(1, best.hit.bits)))
    if margin_hit and not strict:
        # A decisive-margin hit is trustworthy despite the loose distance —
        # floor its confidence so the ranking pipeline treats it as a lock.
        confidence = max(confidence, 0.92)
    return ResolvedCard(
        card_id=None,
        upstream_id=best.hit.upstream_id,
        unified=_unified_from_hit(best.hit),
        source="fingerprint",
        confidence=round(confidence, 3),
        decisive=strict or margin_hit,
    )


# ----------------------------------------------------------------- write side


async def link_external_ref(
    db: AsyncSession,
    *,
    card_id: uuid.UUID,
    source: str,
    external_id: str,
    confidence: float = 1.0,
) -> CardExternalRef:
    """Idempotent upsert into ``card_external_refs``."""
    existing = (
        await db.execute(
            select(CardExternalRef).where(
                CardExternalRef.source == source,
                CardExternalRef.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.card_id = card_id
        existing.confidence = confidence
        return existing
    ref = CardExternalRef(
        card_id=card_id,
        source=source,
        external_id=external_id,
        confidence=confidence,
    )
    db.add(ref)
    return ref


# ----------------------------------------------------------- materialization


_TCG_TO_ENUM = {
    "pokemon": TcgEnum.pokemon,
    "magic": TcgEnum.magic,
    "yugioh": TcgEnum.yugioh,
}


async def _get_or_create_set(
    db: AsyncSession,
    *,
    tcg_enum: TcgEnum,
    set_block: dict[str, Any] | None,
    fallback_name: str | None,
    fallback_code: str | None,
) -> CardSet | None:
    """Idempotently find/create a :class:`CardSet` from upstream payload."""
    code = None
    name = None
    if isinstance(set_block, dict):
        code = set_block.get("code") or None
        name = set_block.get("name") or None
    code = code or fallback_code
    name = name or fallback_name
    if not (code or name):
        return None
    stmt = select(CardSet).where(CardSet.tcg == tcg_enum)
    if code:
        existing = (
            await db.execute(stmt.where(CardSet.code == code).limit(1))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    if name:
        existing = (
            await db.execute(stmt.where(CardSet.name == name).limit(1))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    release_date = None
    if isinstance(set_block, dict):
        rd = set_block.get("release_date")
        if isinstance(rd, str) and len(rd) >= 4:
            try:
                from datetime import date as _date

                parts = rd.split("-")
                release_date = _date(
                    int(parts[0]),
                    int(parts[1]) if len(parts) > 1 else 1,
                    int(parts[2]) if len(parts) > 2 else 1,
                )
            except (TypeError, ValueError):
                release_date = None
    new_set = CardSet(
        tcg=tcg_enum,
        name=name or (code or "Unknown Set"),
        code=code,
        release_date=release_date,
        total_cards=(set_block or {}).get("total_cards")
        if isinstance(set_block, dict)
        else None,
    )
    db.add(new_set)
    await db.flush()
    return new_set


async def ensure_local_card(
    db: AsyncSession,
    *,
    upstream_id: str,
    unified: dict[str, Any] | None = None,
    confidence: float = 1.0,
) -> Card | None:
    """Find or create a local :class:`Card` for an upstream catalog hit.

    The single funnel every "the user just added this card" flow should
    pass through. Idempotent: a second call with the same ``upstream_id``
    returns the same row. Writes a :class:`CardExternalRef` so future
    resolves are O(1).

    Returns ``None`` only when the upstream payload can't be fetched
    *and* no prior ref exists — i.e. we genuinely don't know what card
    this is yet.
    """
    if not upstream_id or ":" not in upstream_id:
        return None
    source, _, external_id = upstream_id.partition(":")
    source = source.lower()
    if not external_id:
        return None

    # 1. Already linked? Return the existing local Card.
    existing_ref = (
        await db.execute(
            select(CardExternalRef).where(
                CardExternalRef.source == source,
                CardExternalRef.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if existing_ref is not None:
        card = await card_catalog_service.get_card(db, existing_ref.card_id)
        if card is not None:
            return card

    # 2. Need to materialize. Fetch upstream payload (if not provided).
    if unified is None:
        unified = await card_search_service.get_card(upstream_id)
    if unified is None:
        return None

    tcg_value = str(unified.get("tcg") or "").lower()
    tcg_enum = _TCG_TO_ENUM.get(tcg_value)
    if tcg_enum is None:
        # Unknown tcg → can't safely materialize (FK enum constraint).
        return None

    card_set = await _get_or_create_set(
        db,
        tcg_enum=tcg_enum,
        set_block=unified.get("set") if isinstance(unified.get("set"), dict) else None,
        fallback_name=unified.get("set_name"),
        fallback_code=unified.get("set_code"),
    )
    if card_set is None:
        # Last-ditch placeholder set so we can still record the card.
        card_set = CardSet(tcg=tcg_enum, name="Unknown Set")
        db.add(card_set)
        await db.flush()

    metadata: dict[str, Any] = {}
    for key in ("pricing_summary", "images", "attributes", "tags", "image_url"):
        val = unified.get(key)
        if val:
            metadata[key] = val

    # Race-safe materialization. Two concurrent requests for the same
    # upstream_id will both pass the "no ref" check above; the unique
    # constraint ``uq_card_external_refs_src_id`` guarantees only one
    # row wins at the DB level. We catch that IntegrityError, roll back
    # the savepoint, and re-read the winner so the loser still returns
    # a valid Card to its caller.
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    try:
        async with db.begin_nested():
            card = Card(
                set_id=card_set.id,
                tcg=tcg_enum,
                name=str(unified.get("name") or "Unknown"),
                number=unified.get("number"),
                rarity=unified.get("rarity"),
                year=unified.get("year"),
                image_url=unified.get("image_url"),
                card_metadata=metadata or None,
            )
            db.add(card)
            await db.flush()
            await link_external_ref(
                db,
                card_id=card.id,
                source=source,
                external_id=external_id,
                confidence=confidence,
            )
            await db.flush()
        return card
    except _IntegrityError:
        # Lost the race — the other transaction inserted the ref first.
        winner_ref = (
            await db.execute(
                select(CardExternalRef).where(
                    CardExternalRef.source == source,
                    CardExternalRef.external_id == external_id,
                )
            )
        ).scalar_one_or_none()
        if winner_ref is None:
            return None
        return await card_catalog_service.get_card(db, winner_ref.card_id)


# ---------------------------------------------------------------- unified API


async def resolve(
    db: AsyncSession,
    *,
    upstream_id: str | None = None,
    query: str | None = None,
    phash: str | None = None,
    uuid_: uuid.UUID | None = None,
    tcg: str | None = None,
    materialize: bool = False,
) -> ResolvedCard | None:
    """One funnel for "given any hint about a card, give me the canonical id".

    Tries (in order): explicit UUID → upstream composite id → pHash →
    free-text. When ``materialize=True`` and the resolution lands on
    an upstream-only hit, also creates the local :class:`Card` so the
    user can attach grades / collection items / alerts to it.
    """
    resolved: ResolvedCard | None = None

    if uuid_ is not None:
        resolved = await resolve_by_uuid(db, uuid_)
    if resolved is None and upstream_id:
        resolved = await resolve_by_upstream_id(db, upstream_id)
    if resolved is None and phash:
        resolved = await resolve_by_phash(db, phash)
    if resolved is None and query:
        resolved = await resolve_by_text(db, query=query, tcg=tcg)
    if resolved is None:
        return None

    if (
        materialize
        and resolved.card_id is None
        and resolved.upstream_id
        and ":" in resolved.upstream_id
    ):
        card = await ensure_local_card(
            db,
            upstream_id=resolved.upstream_id,
            unified=resolved.unified,
            confidence=resolved.confidence,
        )
        if card is not None:
            resolved = ResolvedCard(
                card_id=card.id,
                upstream_id=resolved.upstream_id,
                unified=resolved.unified,
                source=resolved.source,
                confidence=resolved.confidence,
            )
    return resolved


# ------------------------------------------------------------------- helpers


# Preference order for the composite id we surface to clients — providers we
# can use for live pricing first (that's the id the browse/search views key off).
_REF_PRIORITY = {
    "pokemontcg": 0,
    "scryfall": 1,
    "ygoprodeck": 2,
    "tcgplayer": 3,
    "psa": 4,
}


async def upstream_ids_for(
    db: AsyncSession, card_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Map each local card id → its preferred composite upstream id, one query.

    The single batch helper the watchlist / alert lists use to hand each row
    back the ``<source>:<external_id>`` the client can match on without knowing
    the local UUID.
    """
    ids = list(dict.fromkeys(card_ids))  # de-dupe, preserve order
    if not ids:
        return {}
    rows = (
        (
            await db.execute(
                select(CardExternalRef).where(CardExternalRef.card_id.in_(ids))
            )
        )
        .scalars()
        .all()
    )
    best: dict[uuid.UUID, CardExternalRef] = {}
    for r in rows:
        cur = best.get(r.card_id)
        if cur is None or _REF_PRIORITY.get(r.source, 99) < _REF_PRIORITY.get(
            cur.source, 99
        ):
            best[r.card_id] = r
    return {cid: f"{r.source}:{r.external_id}" for cid, r in best.items()}


async def _preferred_external_ref(db: AsyncSession, card_id: uuid.UUID) -> str | None:
    """Pick the most useful upstream id for a single card."""
    return (await upstream_ids_for(db, [card_id])).get(card_id)


async def ensure_local_card_id(db: AsyncSession, ref: str) -> uuid.UUID | None:
    """Resolve a card *reference* — a local ``uuid`` or a composite upstream id
    like ``pokemontcg:base1-4`` — to a local Card UUID, materializing an
    upstream-only card if we don't have it yet.

    The single funnel every "pin/alert whatever the user is looking at" flow
    should use, so clients can pass the id they already have (the browse/search
    view only knows the composite upstream id) and never need a pre-resolve
    round-trip. Returns ``None`` when the reference can't be resolved
    (unknown UUID, non-composite string, or an upstream card we can't fetch).
    """
    if not ref:
        return None
    # A local UUID already? Verify it exists so we never pin a dangling id.
    try:
        cid = uuid.UUID(str(ref))
    except (ValueError, AttributeError):
        cid = None
    if cid is not None:
        row = await card_catalog_service.get_card(db, cid)
        return cid if row is not None else None
    # Composite upstream id → find-or-create the local Card (idempotent).
    if ":" in str(ref):
        card = await ensure_local_card(db, upstream_id=str(ref))
        return card.id if card is not None else None
    return None


def _hamming(a: str, b: str) -> int:
    """Hex-string Hamming distance (per nibble)."""
    if len(a) != len(b):
        return max(len(a), len(b))
    try:
        ia = int(a, 16)
        ib = int(b, 16)
    except ValueError:
        return len(a) * 4
    return bin(ia ^ ib).count("1")


# Re-export the Card model so callers don't double-import.
__all__ = [
    "Card",
    "ResolvedCard",
    "ensure_local_card",
    "ensure_local_card_id",
    "link_external_ref",
    "resolve",
    "resolve_by_phash",
    "resolve_by_text",
    "resolve_by_upstream_id",
    "resolve_by_uuid",
    "upstream_ids_for",
]
