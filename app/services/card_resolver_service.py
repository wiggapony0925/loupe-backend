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

from app.models.card import Card
from app.models.card_external_ref import CardExternalRef
from app.models.fingerprint import Fingerprint
from app.services import card_catalog_service, card_search_service
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


# ------------------------------------------------------------------- helpers


async def _preferred_external_ref(db: AsyncSession, card_id: uuid.UUID) -> str | None:
    """Pick the most useful upstream id for a card.

    Preference order favours providers we can use for live pricing.
    """
    rows = (
        (
            await db.execute(
                select(CardExternalRef).where(CardExternalRef.card_id == card_id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    priority = {
        "pokemontcg": 0,
        "scryfall": 1,
        "ygoprodeck": 2,
        "tcgplayer": 3,
        "psa": 4,
    }
    best = min(rows, key=lambda r: priority.get(r.source, 99))
    return f"{best.source}:{best.external_id}"


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
    "link_external_ref",
    "resolve_by_phash",
    "resolve_by_text",
    "resolve_by_upstream_id",
    "resolve_by_uuid",
]
