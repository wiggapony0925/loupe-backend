"""Storefront category tags — the single backend-driven source both the web
and mobile clients render for the marketplace/search nav.

Today each client hardcodes its own list, and they've drifted (web shows
Digimon but no availability state; mobile stale-marks One Piece "Soon" even
though it now has a cached catalog). Serving one canonical list fixes that and
lets us flip a game live everywhere from the backend — no client release.

``status`` is derived from the *real* catalog capability
(``card_search_service.UNSUPPORTED_TCGS``): a game is ``soon`` until it has a
working provider, then ``live`` automatically. The app-only actions
(Scan, Grade, Scanner) are intentionally NOT here — they're client features,
always present, so each client appends them to its own nav.
"""

from __future__ import annotations

from typing import Any

#: (key, label) in canonical render order; "all" leads the game rail.
_GAMES: list[tuple[str, str]] = [
    ("all", "All Cards"),
    ("pokemon", "Pokémon"),
    ("magic", "Magic"),
    ("yugioh", "Yu-Gi-Oh!"),
    ("onepiece", "One Piece"),
    ("lorcana", "Lorcana"),
    ("digimon", "Digimon"),
    ("sports", "Sports"),
]

#: Content tabs that sit beside the games (routes to their own pages).
_SECTIONS: list[tuple[str, str]] = [
    ("sets", "Sets"),
    ("sealed", "Sealed"),
]


def _game_status(key: str) -> str:
    """``live`` for "all" and any data-backed game; ``soon`` while a game still
    has no catalog provider (i.e. it's in ``UNSUPPORTED_TCGS``)."""
    if key == "all":
        return "live"
    # Lazy import keeps this module import-order-safe (card_search_service is a
    # heavy module) — we only need the constant set.
    from app.services.catalog.card_search_service import UNSUPPORTED_TCGS

    return "soon" if key in UNSUPPORTED_TCGS else "live"


def catalog_tags() -> dict[str, Any]:
    """Canonical marketplace/search tags for both clients."""
    return {
        "tags": [
            {"key": k, "label": lbl, "kind": "game", "status": _game_status(k)}
            for k, lbl in _GAMES
        ]
        + [
            {"key": k, "label": lbl, "kind": "section", "status": "live"}
            for k, lbl in _SECTIONS
        ]
    }


__all__ = ["catalog_tags"]
