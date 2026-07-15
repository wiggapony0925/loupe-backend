"""Loupe AI tuning knobs — every limit and cache policy in one place.

The client-visible limits are SERVER-DRIVEN: ``/v1/app/config`` serves them
(``aiSearch: {queryMaxChars, messageMaxChars}``), so changing a number here
reaches every installed app on its next config refresh — no release.
"""

from __future__ import annotations

#: Hard cap on the assistant's bubble message. The prompt asks for less; the
#: clamp (word-boundary truncated, never a validation failure) is what the
#: clients rely on.
MESSAGE_MAX_CHARS = 400

#: Longest description a user may ask about. The route 422s past this and the
#: client inputs enforce the live value from ``/v1/app/config``.
QUERY_MAX_CHARS = 200

#: Games the model may answer with (mirrors the catalog's supported games).
GAMES: tuple[str, ...] = ("pokemon", "magic", "yugioh", "onepiece", "digimon")

#: Human labels for the game-preference prompt hint.
GAME_LABELS: dict[str, str] = {
    "pokemon": "Pokémon",
    "magic": "Magic: The Gathering",
    "yugioh": "Yu-Gi-Oh!",
    "onepiece": "One Piece",
    "digimon": "Digimon",
}

#: Model answers are deterministic (temperature 0) — cache them a while.
#: Bump the KEY VERSION whenever the prompt changes so a new prompt takes
#: effect immediately instead of after the TTL.
PLAN_TTL = 7 * 24 * 60 * 60
PLAN_CACHE_KEY = "ai_search:plan:v6"

#: Cards fetched per candidate name (interleaved, deduped, then capped).
PER_CANDIDATE = 12

#: Token budget for the plan call (2-3 sentences + five names).
PLAN_MAX_TOKENS = 500

#: Cards fetched per resolved SET when the ask is a collection ("movie
#: promos", "evolving skies alt arts") — set pages are the ground truth for
#: those, so they outrank name-lookup guesses in the interleave.
PER_SET = 16

#: ── Shelf verification (the answer reviews itself before serving) ──
#: For collection asks the model gets the JSON of what we're ABOUT to show
#: (index/name/set) and returns the indexes that truly belong, best first.
#: Runs on the cheap model — verification is far easier than recall.
VERIFY_ENABLED = True
VERIFY_MODEL = "gpt-4o-mini"
VERIFY_MAX_TOKENS = 200
#: How many pooled cards the reviewer sees (and may keep).
VERIFY_POOL = 24
#: Verified orders are cached per (query, game, shelf fingerprint).
VERIFY_TTL = 24 * 60 * 60
VERIFY_CACHE_KEY = "ai_search:verify:v1"

__all__ = [
    "GAMES",
    "GAME_LABELS",
    "MESSAGE_MAX_CHARS",
    "PER_CANDIDATE",
    "PER_SET",
    "PLAN_CACHE_KEY",
    "PLAN_MAX_TOKENS",
    "PLAN_TTL",
    "QUERY_MAX_CHARS",
    "VERIFY_CACHE_KEY",
    "VERIFY_ENABLED",
    "VERIFY_MAX_TOKENS",
    "VERIFY_MODEL",
    "VERIFY_POOL",
    "VERIFY_TTL",
]
