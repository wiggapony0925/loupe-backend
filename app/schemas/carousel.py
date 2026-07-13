"""Carousel recipe schema — the serializable contract for a marketplace shelf.

A *recipe* is a fully data-driven carousel definition over a constrained filter
vocabulary (price band, rarity, sort). It is what the AI generator emits and
what the web compiles into a real rail. Keeping it to a small, validated
vocabulary means an LLM can invent creative shelves safely: it picks the theme,
copy, and filters, but the cards still come from our own cached data.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CarouselRecipe(BaseModel):
    """One AI- or curator-authored carousel. Mirrors the web `CarouselRecipe`."""

    id: str = Field(max_length=48)
    title: str = Field(max_length=60)
    subtitle: str = Field(max_length=120)
    #: Where the cards come from. value/trending use the priced discovery pools;
    #: catalog pulls from the browse catalog (works for catalog-only games).
    source: Literal["value", "trending", "catalog"] = "value"
    priceMin: float | None = Field(default=None, ge=0, le=100_000)
    priceMax: float | None = Field(default=None, ge=0, le=100_000)
    #: Rarity regex source, e.g. "secret|rainbow|illustration".
    rarityPattern: str | None = Field(default=None, max_length=120)
    rarities: list[str] | None = Field(default=None, max_length=12)
    sort: Literal["price_desc", "price_asc", "name"] | None = "price_desc"
    limit: int | None = Field(default=20, ge=4, le=40)
    minItems: int | None = Field(default=4, ge=1, le=12)


class RegistryRecipe(CarouselRecipe):
    """A registry entry: a recipe plus the operator controls layered on it.

    These are what the checked-in JSON registry holds and what operators edit
    from the dev portal. ``games=None`` means "every priced game" (value shelves
    are meaningless for catalog-only games); an explicit list scopes verbatim.
    """

    enabled: bool = True
    games: list[str] | None = Field(default=None, max_length=12)


class CarouselOverrides(BaseModel):
    """Operator edits layered over the file registry at serve time.

    Stored as one JSON document in ``kv_cache`` (shared Postgres — every
    instance sees a save immediately, no migration needed): field patches for
    file recipes, whole operator-added recipes, tombstones for deleted file
    recipes, and the AI-shelf kill switch.
    """

    edits: dict[str, dict[str, Any]] = Field(default_factory=dict)
    added: list[RegistryRecipe] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    aiEnabled: bool = True


class AdminRecipe(RegistryRecipe):
    """A merged registry entry annotated for the dev portal."""

    #: "file" = checked-in registry entry; "custom" = operator-added.
    origin: Literal["file", "custom"] = "file"
    #: A file recipe with a live operator patch on it.
    edited: bool = False
    #: A file recipe the operator deleted — kept in the admin view (restorable
    #: via reset) but never served.
    removed: bool = False


class RecipeUpdate(BaseModel):
    """Partial edit for one recipe — only fields the operator actually sent
    (``exclude_unset``) are patched, so ``games: null`` really means "all
    priced games" rather than "unchanged"."""

    enabled: bool | None = None
    title: str | None = Field(default=None, max_length=60)
    subtitle: str | None = Field(default=None, max_length=120)
    source: Literal["value", "trending", "catalog"] | None = None
    priceMin: float | None = Field(default=None, ge=0, le=100_000)
    priceMax: float | None = Field(default=None, ge=0, le=100_000)
    rarityPattern: str | None = Field(default=None, max_length=120)
    rarities: list[str] | None = Field(default=None, max_length=12)
    sort: Literal["price_desc", "price_asc", "name"] | None = None
    limit: int | None = Field(default=None, ge=4, le=40)
    minItems: int | None = Field(default=None, ge=1, le=12)
    games: list[str] | None = Field(default=None, max_length=12)


class GameCarouselSummary(BaseModel):
    """Per-game serve preview for the dev portal."""

    id: str
    label: str
    catalogOnly: bool
    #: Enabled registry recipes that would serve for this game right now.
    curatedCount: int
    #: Shelf count of today's cached AI design, if one exists.
    aiCount: int | None = None
    #: What ``/v1/public/carousels`` would answer with right now.
    activeSource: Literal["ai", "curated"]
    #: Rail count of the cached resolved payload (what clients render), if warm.
    resolvedRails: int | None = None


class AdminCarouselsView(BaseModel):
    """Everything the /admin/carousels page renders in one call."""

    aiConfigured: bool
    aiEnabled: bool
    recipes: list[AdminRecipe]
    games: list[GameCarouselSummary]
    #: Latest AI-designed shelves per game (today's cache), for the AI panel.
    ai: dict[str, list[CarouselRecipe]] = Field(default_factory=dict)


class CarouselResponse(BaseModel):
    """A game's generated shelves + where they came from (for the UI badge)."""

    game: str
    source: Literal["ai", "curated"]
    carousels: list[CarouselRecipe]


class ResolvedRail(BaseModel):
    """A carousel *already resolved into cards* server-side.

    Unlike ``CarouselRecipe`` (a filter definition the client must compile),
    this carries the final title/subtitle (``{label}`` already interpolated) and
    the actual card dicts — so web and mobile render the SAME rail with zero
    client-side filtering. ``cards`` are the wire card shape emitted by the
    shelf/browse services (``pricing_summary``, ``images``, etc.).
    """

    id: str
    title: str
    subtitle: str
    #: "cards" = a priced discovery rail; "catalog" = a browse-the-catalog rail.
    kind: Literal["cards", "catalog"] = "cards"
    cards: list[dict[str, Any]]


class ResolvedCarousels(BaseModel):
    """The ordered, ready-to-render rails for a game — the single source of
    truth both clients render. Empty rails are already dropped server-side."""

    game: str
    source: Literal["ai", "curated"]
    rails: list[ResolvedRail]
