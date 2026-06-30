"""Card data lineage — the single source of truth for WHERE every field of the
unified Card / Set model comes from, plus the ordered price-fallback chain.

This is the "family tree" of the catalog: each card in Loupe is assembled from
one catalog provider (name / set / art / rarity …) and a price resolved through
an ordered fallback across many price providers. Declaring that graph in one
place keeps it honest and makes the system **scalable** — lighting up a new card
API is a single :class:`CatalogSource` entry here (plus its client + the dispatch
in ``card_search_service`` / ``catalog_browse_service``). The ``/admin/card-tree``
visualization reads straight from this module, so the portal always matches code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CatalogSource:
    """A provider that supplies catalog (identity) data for one or more games."""

    id: str  # matches the ``UnifiedCard.source`` prefix (e.g. "apitcg-onepiece")
    label: str  # human-friendly name
    url: str  # provider site / API docs
    games: tuple[str, ...]  # internal tcg keys it serves
    provides: tuple[str, ...]  # unified Card fields it populates
    embedded_price: bool  # does its catalog payload carry a price?
    key_required: bool  # needs an API key to call?


# Identity fields every catalog provider is expected to supply.
_IDENTITY_FIELDS: tuple[str, ...] = (
    "name",
    "set_name",
    "set_code",
    "number",
    "rarity",
    "image_url",
    "images",
    "attributes",
)

#: The catalog provider graph. ONE entry per provider — add a card API here.
CATALOG_SOURCES: tuple[CatalogSource, ...] = (
    CatalogSource(
        id="pokemontcg",
        label="Pokémon TCG API",
        url="https://pokemontcg.io",
        games=("pokemon",),
        provides=(*_IDENTITY_FIELDS, "year", "pricing_summary"),
        embedded_price=True,
        key_required=True,
    ),
    CatalogSource(
        id="scryfall",
        label="Scryfall",
        url="https://scryfall.com/docs/api",
        games=("magic",),
        provides=(*_IDENTITY_FIELDS, "year", "pricing_summary"),
        embedded_price=True,
        key_required=False,
    ),
    CatalogSource(
        id="ygoprodeck",
        label="YGOPRODeck",
        url="https://ygoprodeck.com/api-guide/",
        games=("yugioh",),
        provides=(*_IDENTITY_FIELDS, "pricing_summary"),
        embedded_price=True,
        key_required=False,
    ),
    CatalogSource(
        id="digimoncard",
        label="digimoncard.io",
        url="https://documenter.getpostman.com/view/14059948",
        games=("digimon",),
        provides=_IDENTITY_FIELDS,
        embedded_price=False,
        key_required=False,
    ),
    CatalogSource(
        id="apitcg-onepiece",
        label="apitcg.com",
        url="https://docs.apitcg.com",
        games=("onepiece",),
        provides=_IDENTITY_FIELDS,
        embedded_price=False,
        key_required=True,
    ),
)

GAME_LABELS: dict[str, str] = {
    "pokemon": "Pokémon",
    "magic": "Magic",
    "yugioh": "Yu-Gi-Oh!",
    "digimon": "Digimon",
    "onepiece": "One Piece",
}

# Human notes for each unified-model field's origin, keyed by field name.
_CARD_FIELD_ORIGINS: tuple[tuple[str, str, str], ...] = (
    ("name", "catalog", "The game's catalog provider"),
    ("set_name", "catalog", "Catalog provider (Digimon/One Piece derived by id)"),
    ("set_code", "catalog", "Catalog provider / card id prefix"),
    ("number", "catalog", "Catalog provider"),
    ("rarity", "catalog", "Catalog provider"),
    ("image_url", "catalog", "Catalog provider"),
    ("year", "catalog", "Catalog provider (where dated)"),
    ("attributes", "catalog", "Catalog provider (game-specific fields)"),
    (
        "pricing_summary",
        "price-chain",
        "Embedded catalog price if present, else the ordered price fallback",
    ),
    ("source", "loupe", "Provenance — which catalog provider answered"),
    ("metadata", "loupe", "Sync timestamp + confidence"),
)

_SET_FIELD_ORIGINS: tuple[tuple[str, str, str], ...] = (
    ("name", "catalog", "Catalog provider (Digimon/One Piece from a name map)"),
    ("code", "catalog", "Catalog provider / id prefix"),
    ("tcg", "loupe", "The game key"),
    ("release_date", "catalog", "Catalog provider (where available)"),
    ("total_cards", "catalog", "Catalog provider or derived count"),
    ("image_url", "catalog", "Catalog provider set logo (where available)"),
)


def build_card_tree() -> dict[str, Any]:
    """Assemble the full lineage tree (catalog sources, per-game routing, the
    price fallback chain with live configured-state, and per-field origins)."""
    from app.integrations.registry import _PRICE_SOURCE_PRIORITY, get_registry

    registry = get_registry()
    configured = {p.id for p in registry.all if p.is_configured()}

    price_chain = [
        {"order": i + 1, "id": sid, "configured": sid in configured}
        for i, sid in enumerate(_PRICE_SOURCE_PRIORITY)
    ]

    games: list[dict[str, Any]] = []
    for src in CATALOG_SOURCES:
        for g in src.games:
            games.append(
                {
                    "tcg": g,
                    "label": GAME_LABELS.get(g, g.title()),
                    "catalog_source": src.id,
                    "catalog_label": src.label,
                    "price": "embedded → fallback chain"
                    if src.embedded_price
                    else "fallback chain",
                }
            )

    def _origins(rows: tuple[tuple[str, str, str], ...]) -> list[dict[str, str]]:
        return [{"field": f, "from": frm, "note": note} for f, frm, note in rows]

    return {
        "card_model": {"name": "UnifiedCard", "fields": _origins(_CARD_FIELD_ORIGINS)},
        "set_model": {"name": "UnifiedSet", "fields": _origins(_SET_FIELD_ORIGINS)},
        "catalog_sources": [
            {
                "id": s.id,
                "label": s.label,
                "url": s.url,
                "games": [GAME_LABELS.get(g, g.title()) for g in s.games],
                "game_keys": list(s.games),
                "provides": list(s.provides),
                "embedded_price": s.embedded_price,
                "key_required": s.key_required,
            }
            for s in CATALOG_SOURCES
        ],
        "games": games,
        "price_chain": price_chain,
    }


__all__ = ["CATALOG_SOURCES", "CatalogSource", "build_card_tree"]
