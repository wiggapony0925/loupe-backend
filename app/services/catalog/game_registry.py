"""Canonical trading-card game registry — the ONE source of truth.

The game set (keys, display labels, unified catalog source, and availability)
was redefined in a half-dozen places that had drifted apart: `_source_for` and
`UNSUPPORTED_TCGS` in card_search_service, the `_GAMES` label list in games.py,
and eight divergent `tcg` validation regexes across the routers. Everything now
derives from :data:`GAMES` here, so adding a game or flipping one live is a
one-line change.

Pure module — no heavy imports — so anything can import it without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Game:
    key: str
    label: str
    #: Unified catalog source id (matches the `source` on UnifiedCard).
    source: str
    #: Has a working catalog provider today. False ⇒ the client shows "Soon".
    supported: bool


#: Canonical render order; "all" is a cross-provider pseudo-game handled by
#: callers, so it is NOT a member here (see `source_for` / `tcg_keys`).
GAMES: tuple[Game, ...] = (
    Game("pokemon", "Pokémon", "pokemontcg", True),
    Game("magic", "Magic", "scryfall", True),
    Game("yugioh", "Yu-Gi-Oh!", "ygoprodeck", True),
    Game("onepiece", "One Piece", "apitcg-onepiece", True),
    Game("digimon", "Digimon", "digimoncard", True),
    Game("lorcana", "Lorcana", "lorcana", False),
    Game("sports", "Sports", "sports", False),
)

_BY_KEY: dict[str, Game] = {g.key: g for g in GAMES}

#: All game keys in canonical order (no "all").
GAME_KEYS: tuple[str, ...] = tuple(g.key for g in GAMES)
#: Games with a working provider (searchable today).
SUPPORTED_KEYS: frozenset[str] = frozenset(g.key for g in GAMES if g.supported)
#: Games marketed but not yet data-backed (client shows "Soon").
UNSUPPORTED_KEYS: frozenset[str] = frozenset(g.key for g in GAMES if not g.supported)


def source_for(tcg: str) -> str:
    """Unified catalog source id for a tcg key (``all`` → ``mixed``)."""
    if tcg == "all":
        return "mixed"
    game = _BY_KEY.get(tcg)
    return game.source if game else tcg


def label_for(tcg: str) -> str:
    game = _BY_KEY.get(tcg)
    return game.label if game else tcg.title()


def tcg_pattern(*, allow_all: bool = True, supported_only: bool = False) -> str:
    """A FastAPI ``Query(pattern=…)`` string for a ``tcg``/``game`` param.

    Derive every endpoint's validation from this so the accepted set can't drift
    per-route (it had drifted eight ways). ``supported_only`` restricts to
    data-backed games for endpoints that need a real catalog (browse sets,
    carousels, trending); the default accepts every marketed game (search
    endpoints degrade unsupported games to an empty result, not a 422).
    ``allow_all`` includes the ``all`` cross-provider pseudo-game.
    """
    # Derive from the ordered GAMES tuple (not the frozensets) so the produced
    # regex string is deterministic — the OpenAPI snapshot depends on it.
    base = [g.key for g in GAMES if not supported_only or g.supported]
    keys = [*base, "all"] if allow_all else base
    return "^(" + "|".join(keys) + ")$"


__all__ = [
    "GAMES",
    "GAME_KEYS",
    "SUPPORTED_KEYS",
    "UNSUPPORTED_KEYS",
    "Game",
    "label_for",
    "source_for",
    "tcg_pattern",
]
