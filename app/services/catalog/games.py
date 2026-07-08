"""Storefront category tags — the single backend-driven source both the web
and mobile clients render for the marketplace/search nav.

Today each client hardcodes its own list, and they've drifted (web shows
Digimon but no availability state; mobile stale-marks One Piece "Soon" even
though it now has a cached catalog). Serving one canonical list fixes that and
lets us flip a game live everywhere from the backend — no client release.

``status`` is derived from the *real* catalog capability
(``game_registry.GAMES`` — the one source of truth also behind
``card_search_service``): a game is ``soon`` until it has a working provider,
then ``live`` automatically. The app-only actions
(Scan, Grade, Scanner) are intentionally NOT here — they're client features,
always present, so each client appends them to its own nav.
"""

from __future__ import annotations

from typing import Any

from app.services.catalog import game_registry

#: "All Cards" leads the game rail — a cross-provider pseudo-game that isn't a
#: registry member (it has no single source), so it's prepended here.
_ALL_TAG = {"key": "all", "label": "All Cards", "kind": "game", "status": "live"}

#: Content tabs that sit beside the games (routes to their own pages).
_SECTIONS: list[tuple[str, str]] = [
    ("sets", "Sets"),
    ("sealed", "Sealed"),
]


def catalog_tags() -> dict[str, Any]:
    """Canonical marketplace/search tags for both clients. Games + their
    ``live``/``soon`` status come straight off the game registry, so flipping a
    game live (``supported=True``) updates every client from one place."""
    game_tags = [
        {
            "key": g.key,
            "label": g.label,
            "kind": "game",
            "status": "live" if g.supported else "soon",
        }
        for g in game_registry.GAMES
    ]
    return {
        "tags": [
            _ALL_TAG,
            *game_tags,
            *(
                {"key": k, "label": lbl, "kind": "section", "status": "live"}
                for k, lbl in _SECTIONS
            ),
        ]
    }


__all__ = ["catalog_tags"]
