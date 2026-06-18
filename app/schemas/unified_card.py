"""Typed ``UnifiedCard`` — the single authoritative shape for live catalog
results.

The catalog search service (:mod:`app.services.catalog.card_search_service`)
adapts three upstream providers (Pokémon TCG, Scryfall, YGOPRODeck) into one
common shape consumed by ``GET /cards/search`` and ``GET /cards/{id}`` and
fed into :func:`compose_canonical_card`.

Historically that shape was a loose ``dict[str, Any]`` built by hand in each
adapter, which let the three adapters drift and gave downstream consumers no
contract to rely on. These models pin the shape down in one place.

The wire format is unchanged: the service still emits plain dicts (via
``.model_dump()``) so the cache JSON, the no-``response_model`` search
endpoint, and every existing ``.get(...)`` consumer keep working byte-for-byte.
Every field is optional because upstream payloads are wildly inconsistent and
the catalog must degrade gracefully rather than reject a partial card.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class UnifiedImage(BaseModel):
    """A single image asset. ``url`` is the only field upstreams reliably give."""

    model_config = ConfigDict(extra="forbid")

    url: str
    width: int | None = None
    height: int | None = None
    alt: str | None = None


class UnifiedImageSet(BaseModel):
    """The four image renditions the frontend knows how to pick from."""

    model_config = ConfigDict(extra="forbid")

    small: UnifiedImage | None = None
    normal: UnifiedImage | None = None
    large: UnifiedImage | None = None
    art_crop: UnifiedImage | None = None


class UnifiedMoney(BaseModel):
    """A money amount in a single currency."""

    model_config = ConfigDict(extra="forbid")

    amount: float
    currency: str = "USD"


class UnifiedPricingSummary(BaseModel):
    """Best-effort catalog pricing snapshot (TCGplayer / Cardmarket / etc.)."""

    model_config = ConfigDict(extra="forbid")

    card_id: str | None = None
    currency: str = "USD"
    market: UnifiedMoney | None = None
    low: UnifiedMoney | None = None
    mid: UnifiedMoney | None = None
    high: UnifiedMoney | None = None
    as_of: str | None = None
    sample_size: int | None = None
    sources: list[str] | None = None


class UnifiedSet(BaseModel):
    """The set/expansion a card belongs to."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    code: str | None = None
    name: str | None = None
    series: str | None = None
    release_date: str | None = None
    printed_total: int | None = None
    total_cards: int | None = None
    logo: UnifiedImage | None = None
    symbol: UnifiedImage | None = None


class UnifiedMetadata(BaseModel):
    """Provenance for the catalog record itself."""

    model_config = ConfigDict(extra="forbid")

    source: str
    last_synced_at: str | None = None
    confidence: float | None = None


class UnifiedCard(BaseModel):
    """One catalog card, normalized across every upstream provider.

    ``attributes`` stays free-form on purpose: each TCG exposes a different,
    open-ended bag of game-specific fields (HP/attacks for Pokémon, mana cost
    for Magic, ATK/DEF for Yu-Gi-Oh) that no fixed schema should pin down.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    tcg: str
    source: str
    set_name: str | None = None
    set_code: str | None = None
    number: str | None = None
    rarity: str | None = None
    image_url: str | None = None
    year: int | None = None
    images: UnifiedImageSet | None = None
    set: UnifiedSet | None = None
    attributes: dict[str, Any] | None = None
    pricing_summary: UnifiedPricingSummary | None = None
    tags: list[str] | None = None
    metadata: UnifiedMetadata | None = None


__all__ = [
    "UnifiedCard",
    "UnifiedImage",
    "UnifiedImageSet",
    "UnifiedMetadata",
    "UnifiedMoney",
    "UnifiedPricingSummary",
    "UnifiedSet",
]
