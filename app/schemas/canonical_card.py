"""Canonical Card — the "holy grail" cross-provider JSON shape.

A single, frozen schema that every upstream catalog (Pokémon TCG,
Scryfall, YGOPRODeck) and every price/comp/population provider
(eBay, 130point, PSA, TCGplayer, PriceCharting, Pokémon TCG API,
TCGCSV, JustTCG, GoCollect) is composed into.

Frontend renders one card view from this exact shape. Contract tests
in ``tests/test_canonical_card_*.py`` lock down:

  * the schema itself (this file)
  * each provider → canonical fragment mapping
  * the ``GET /v1/cards/{id}/canonical`` response envelope

Every section is optional — providers only fill in what they have.
``provenance`` records which sources contributed which sections so the
client can show real-vs-synthesized data badges.

Version is bumped by hand on breaking changes; minor field additions
do **not** require a bump.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CANONICAL_CARD_VERSION = "1.0.0"

ProvenanceKind = Literal["real", "synthesized", "cached", "missing"]


# --------------------------------------------------------------------- atoms


class Money(BaseModel):
    """Currency-tagged amount."""

    model_config = ConfigDict(extra="forbid")
    amount: float
    currency: str = "USD"


class ImageAsset(BaseModel):
    """One renderable image variant."""

    model_config = ConfigDict(extra="forbid")
    url: str
    width: int | None = None
    height: int | None = None
    alt: str | None = None


class ImageSet(BaseModel):
    """All known image variants for a card. All optional."""

    model_config = ConfigDict(extra="forbid")
    small: ImageAsset | None = None
    normal: ImageAsset | None = None
    large: ImageAsset | None = None
    art_crop: ImageAsset | None = None


# --------------------------------------------------------------------- core


class CanonicalIdentity(BaseModel):
    """Stable identity for a card across providers."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., description="Composite id, e.g. 'pokemontcg:base1-4' or UUID")
    name: str
    tcg: str = Field(..., description="pokemon | magic | yugioh | onepiece | lorcana | sports")
    number: str | None = None
    rarity: str | None = None
    year: int | None = None
    language: str = "en"
    variant: str | None = None
    finish: str | None = None  # holo, reverse_holo, foil, etc.
    tags: list[str] = Field(default_factory=list)


class CanonicalSet(BaseModel):
    """Set / expansion the card belongs to."""

    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    code: str | None = None
    name: str | None = None
    series: str | None = None
    release_date: str | None = None  # ISO date
    printed_total: int | None = None
    total_cards: int | None = None
    logo: ImageAsset | None = None
    symbol: ImageAsset | None = None


# --------------------------------------------------------------------- pricing


class PriceQuote(BaseModel):
    """One provider's ungraded market quote."""

    model_config = ConfigDict(extra="forbid")
    source: str  # provider id: tcgplayer / cardmarket / pokemontcg / tcgcsv / justtcg / pricecharting
    market: Money | None = None
    low: Money | None = None
    mid: Money | None = None
    high: Money | None = None
    as_of: str | None = None
    sample_size: int | None = None
    url: str | None = None


class GradedPriceRow(BaseModel):
    """One (house, grade) market row — real if from a real source,
    synthesized when the only available data is the raw price."""

    model_config = ConfigDict(extra="forbid")
    house: str  # psa | cgc | bgs | sgc | tag
    grade: float
    grade_label: str
    market: Money
    population: int | None = None
    pop_higher: int | None = None
    change_pct: float | None = None
    last_sale_at: str | None = None
    listing_url: str | None = None
    source: ProvenanceKind = "synthesized"


class CanonicalPricing(BaseModel):
    """All pricing for the card — raw quotes + per-house graded table."""

    model_config = ConfigDict(extra="forbid")
    consensus: Money | None = Field(
        default=None,
        description="Single best-guess market price (median of quotes) for the frontend headline.",
    )
    currency: str = "USD"
    quotes: list[PriceQuote] = Field(default_factory=list)
    graded: list[GradedPriceRow] = Field(default_factory=list)
    summary: dict[str, float | int | str | None] = Field(
        default_factory=dict,
        description="Headline figures: raw, graded_avg, pop_top, pop_total, change_pct_1y, last_sale_at.",
    )


# --------------------------------------------------------------------- population


class PopulationRow(BaseModel):
    """One (house, grade) population row from a real grading-pop source."""

    model_config = ConfigDict(extra="forbid")
    source: str
    house: str
    grade: str
    population: int
    pop_higher: int | None = None


class CanonicalPopulation(BaseModel):
    """All population data across grading houses."""

    model_config = ConfigDict(extra="forbid")
    rows: list[PopulationRow] = Field(default_factory=list)
    total: int = 0
    by_house: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------------- market


class CanonicalListing(BaseModel):
    """One live for-sale listing."""

    model_config = ConfigDict(extra="forbid")
    source: str
    title: str
    price: Money
    url: str
    condition: str | None = None
    image_url: str | None = None
    is_auction: bool = False
    time_left_seconds: int | None = None


class CanonicalComp(BaseModel):
    """One completed sale."""

    model_config = ConfigDict(extra="forbid")
    source: str
    title: str
    price: Money
    sold_at: str  # ISO 8601 UTC
    condition: str | None = None
    grade: str | None = None
    house: str | None = None
    url: str | None = None
    image_url: str | None = None


# --------------------------------------------------------------------- cert


class CanonicalCert(BaseModel):
    """Verified grading cert (PSA today; CGC/BGS when their APIs ship)."""

    model_config = ConfigDict(extra="forbid")
    house: str  # psa | cgc | bgs | sgc
    cert_number: str
    grade: str | None = None
    subject: str | None = None
    year: str | None = None
    brand: str | None = None
    category: str | None = None
    verified_at: str | None = None


# --------------------------------------------------------------------- attrs


class CanonicalAttributes(BaseModel):
    """TCG-specific structured attributes (free-form by design)."""

    model_config = ConfigDict(extra="allow")
    types: list[str] | None = None
    hp: int | None = None
    mana_cost: str | None = None
    type_line: str | None = None
    oracle_text: str | None = None
    abilities: list[dict] | None = None
    attacks: list[dict] | None = None


# --------------------------------------------------------------------- provenance


class CanonicalProvenance(BaseModel):
    """Records who contributed each section. Drives "real vs synthesized" UI."""

    model_config = ConfigDict(extra="forbid")
    identity_source: str | None = None
    set_source: str | None = None
    image_source: str | None = None
    pricing_sources: list[str] = Field(default_factory=list)
    population_sources: list[str] = Field(default_factory=list)
    listings_sources: list[str] = Field(default_factory=list)
    comps_sources: list[str] = Field(default_factory=list)
    cert_sources: list[str] = Field(default_factory=list)
    composed_at: str
    errors: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- root


class CanonicalCard(BaseModel):
    """The single, unified card object. Every Loupe card endpoint
    that wants to expose the "full picture" returns this exact shape."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = CANONICAL_CARD_VERSION
    identity: CanonicalIdentity
    set: CanonicalSet | None = None
    images: ImageSet | None = None
    attributes: CanonicalAttributes | None = None
    pricing: CanonicalPricing = Field(default_factory=CanonicalPricing)
    population: CanonicalPopulation = Field(default_factory=CanonicalPopulation)
    listings: list[CanonicalListing] = Field(default_factory=list)
    comps: list[CanonicalComp] = Field(default_factory=list)
    certs: list[CanonicalCert] = Field(default_factory=list)
    provenance: CanonicalProvenance


__all__ = [
    "CANONICAL_CARD_VERSION",
    "CanonicalAttributes",
    "CanonicalCard",
    "CanonicalCert",
    "CanonicalComp",
    "CanonicalIdentity",
    "CanonicalListing",
    "CanonicalPopulation",
    "CanonicalPricing",
    "CanonicalProvenance",
    "CanonicalSet",
    "GradedPriceRow",
    "ImageAsset",
    "ImageSet",
    "Money",
    "PopulationRow",
    "PriceQuote",
    "ProvenanceKind",
]
