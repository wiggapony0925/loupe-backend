"""Prompt builders for Loupe AI.

Prompts live apart from the transport (:mod:`providers`) and orchestration
(:mod:`search`) so copy tweaks never touch client code — and remember to bump
``config.PLAN_CACHE_KEY`` whenever the wording changes, or week-old cached
answers keep serving the old prompt.
"""

from __future__ import annotations

from app.services.ai.config import GAME_LABELS, MESSAGE_MAX_CHARS

_SEARCH_SYSTEM = f"""You are the search brain of a trading-card marketplace \
(Pokémon, Magic: The Gathering, Yu-Gi-Oh!, One Piece, Digimon).

The user DESCRIBES a card instead of naming it. Map the description to the \
most likely REAL card names. Return ONLY a JSON object, no prose, no code \
fences:
{{
  "message": "2-3 friendly sentences (<= {MESSAGE_MAX_CHARS} characters \
total): say what you think they're describing and WHY it fits, then mention \
the strongest alternates in passing. Written to the user, e.g. \\"A red \
lizard with fire sounds like Charizard — the flame on its tail and the wings \
give it away. If it looked younger it might be Charmeleon or Charmander, so \
I've included those too.\\"",
  "game": "pokemon" | "magic" | "yugioh" | "onepiece" | "digimon" | null,
  "candidates": ["Most likely card name", ... up to 5, best guess first]
}}

Rules:
- candidates are card NAMES only (no set names, no prices, no ids).
- Use null for game unless the description clearly implies one.
- If the description is too vague, still give your best guesses.
- Never invent names: prefer famous, real cards that match the description."""


def search_system_prompt(game_hint: str | None = None) -> str:
    """The "describe it" system prompt, biased by the user's game preference.

    The hint comes from the game tag the user has selected in the search UI —
    "the user is mostly describing a Pokémon card" — so the model resolves
    ambiguous descriptions ("blue dragon") toward the game they're browsing
    while staying free to override when the description clearly points away.
    """
    if not game_hint:
        return _SEARCH_SYSTEM
    label = GAME_LABELS.get(game_hint, game_hint.title())
    return (
        _SEARCH_SYSTEM
        + f"\n\nContext: the user is browsing the {label} section, so they are "
        f"most likely describing a {label} card. Prefer {label} candidates "
        f'and set "game" to "{game_hint}" unless the description clearly '
        "points to another game."
    )


__all__ = ["search_system_prompt"]
