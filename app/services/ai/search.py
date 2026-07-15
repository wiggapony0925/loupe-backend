"""The Loupe AI "describe it" search orchestrator.

"red lizard with fire" → the model maps the description to real card NAMES
plus a short friendly message (:mod:`prompts` → :mod:`providers` →
:mod:`schemas`); the actual cards then come from our own catalog search. The
model never sees or invents card data — every result is a real, priced,
tappable catalog card.

Fast + cheap by construction:
* one small model call (temperature 0, tiny JSON) per NEW question;
* the answer is cached in ``kv_cache`` keyed by (folded query, game hint) —
  repeat questions from ANY user skip the model entirely;
* candidate name lookups run in parallel against the mirror-backed search.

The router gates this behind Loupe Pro and falls back to the normal search
when this returns ``None`` — the user always gets results.
"""

from __future__ import annotations

import asyncio
import unicodedata
from typing import Any

from pydantic import ValidationError

from app.platform.cache_l2 import kv_get, kv_set
from app.services.ai import providers
from app.services.ai.config import PER_CANDIDATE, PLAN_CACHE_KEY, PLAN_TTL
from app.services.ai.prompts import search_system_prompt
from app.services.ai.schemas import AiSearchPlan, parse_plan
from app.utils.logger import get_logger

logger = get_logger("services.ai.search")


def _fold(q: str) -> str:
    return (
        unicodedata.normalize("NFKD", q).encode("ascii", "ignore").decode().lower()
    ).strip()


def _plan_cache_key(q: str, game_hint: str | None) -> str:
    # The hint changes the prompt, so it must key the cache too.
    return f"{PLAN_CACHE_KEY}:{game_hint or 'all'}:{_fold(q)[:120]}"


async def _plan_for(q: str, game_hint: str | None) -> AiSearchPlan | None:
    """The (cached) model answer for a question; ``None`` when unavailable."""
    cache_key = _plan_cache_key(q, game_hint)
    raw = await kv_get(cache_key)
    if raw:
        try:
            return AiSearchPlan.model_validate_json(raw)
        except ValidationError:
            pass  # stale shape → re-ask

    try:
        text = await providers.ask(search_system_prompt(game_hint), q)
    except Exception as exc:  # model/network best effort — callers fall back
        logger.warning("ai search model call failed: %s", exc)
        return None
    if text is None:  # no provider configured
        return None

    plan = parse_plan(text)
    if plan is not None:
        await kv_set(cache_key, plan.model_dump_json(), PLAN_TTL)
    return plan


async def _cards_for(name: str, game: str | None) -> list[dict[str, Any]]:
    from app.services.catalog import card_search_service

    try:
        body = await card_search_service.search_cards(
            q=name, tcg=game or "all", limit=PER_CANDIDATE
        )
    except Exception as exc:  # pragma: no cover - upstream best effort
        logger.debug("ai search candidate lookup failed name=%s: %s", name, exc)
        return []
    results = body.get("results")
    return list(results) if isinstance(results, list) else []


def _interleave(
    per_candidate: list[list[dict[str, Any]]], limit: int
) -> list[dict[str, Any]]:
    """Round-robin merge, preserving candidate rank, deduped by card id."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tier in range(PER_CANDIDATE):
        for cards in per_candidate:
            if tier < len(cards):
                card = cards[tier]
                cid = str(card.get("id"))
                if cid in seen:
                    continue
                seen.add(cid)
                out.append(card)
                if len(out) >= limit:
                    return out
    return out


async def ai_search(
    q: str, limit: int = 24, game_hint: str | None = None
) -> dict[str, Any] | None:
    """Answer a described-card question with REAL catalog cards.

    ``game_hint`` is the game tag active in the search UI — it biases the
    prompt ("they're most likely describing a Pokémon card") and scopes the
    candidate lookups when the model doesn't name a game itself. Returns
    ``None`` when no model is configured / the model failed — the router
    falls back to the normal search so the user always gets results.
    """
    plan = await _plan_for(q, game_hint)
    if plan is None:
        return None
    lookup_game = plan.game or game_hint
    per_candidate = await asyncio.gather(
        *(_cards_for(name, lookup_game) for name in plan.candidates)
    )
    results = _interleave(list(per_candidate), limit)
    return {
        "query": q,
        "message": plan.message,
        "candidates": plan.candidates,
        "game": lookup_game,
        "results": results,
        "total": len(results),
        "source": "ai",
    }


__all__ = ["ai_search"]
