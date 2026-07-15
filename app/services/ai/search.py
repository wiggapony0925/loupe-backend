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
import re
import unicodedata
from typing import Any

from pydantic import ValidationError

from app.platform.cache_l2 import kv_get, kv_set
from app.services.ai import health, providers, verify
from app.services.ai.config import (
    PER_CANDIDATE,
    PER_SET,
    PLAN_CACHE_KEY,
    PLAN_TTL,
    VERIFY_POOL,
)
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


async def _plan_for(q: str, game_hint: str | None) -> tuple[AiSearchPlan | None, bool]:
    """The model answer for a question, as ``(plan, was_cached)``."""
    cache_key = _plan_cache_key(q, game_hint)
    raw = await kv_get(cache_key)
    if raw:
        try:
            return AiSearchPlan.model_validate_json(raw), True
        except ValidationError:
            pass  # stale shape → re-ask

    try:
        text = await providers.ask(search_system_prompt(game_hint), q)
    except Exception as exc:  # model/network best effort — callers fall back
        logger.warning("ai search model call failed: %s", exc)
        # Cool the feature down fleet-wide (quota errors hide it for hours).
        await health.record_failure(exc)
        return None, False
    if text is None:  # no provider configured
        return None, False

    plan = parse_plan(text)
    if plan is not None:
        await kv_set(cache_key, plan.model_dump_json(), PLAN_TTL)
    return plan, False


def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _belongs_to(candidate: str, card_name: str) -> bool:
    """Does a looked-up card actually match the candidate the model named?

    ASYMMETRIC on purpose: the card must contain the candidate, never the
    reverse — "Ancient Mew" accepts "Ancient Mew (Movie Promo)" but a plain
    "Mew" must NOT ride in on the shared word (the random-Mew bug).
    Parentheticals on the candidate are dropped first, so a chatty model
    ("Ancient Mew (Movie Promo)") still matches the printed name.
    """
    candidate = re.sub(r"\(.*?\)", " ", candidate)
    e, c = _norm_name(candidate), _norm_name(card_name)
    if not e or not c:
        return False
    if e in c:
        return True
    et, ct = set(e.split()), set(c.split())
    return et <= ct


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
    cards = list(results) if isinstance(results, list) else []
    # Relevance guard: the answer must contain what the model NAMED, not
    # whatever fuzzy neighbors the search stirred up. Drop rows whose name
    # doesn't match this candidate — but never filter down to nothing (a
    # loose match beats an empty answer).
    kept = [c for c in cards if _belongs_to(name, str(c.get("name") or ""))]
    if kept and len(kept) < len(cards):
        logger.debug(
            "ai search relevance guard candidate=%r kept=%d/%d",
            name,
            len(kept),
            len(cards),
        )
    return kept or cards


async def _resolve_sets(phrases: list[str], game: str | None) -> list[dict[str, Any]]:
    """The model's set names, resolved against the REAL set catalog.

    Suggestions, never trusted as ids — unresolvable phrases drop out."""
    from app.services.catalog import search_intel

    async def one(phrase: str) -> dict[str, Any] | None:
        try:
            return await search_intel.resolve_set(phrase, game)
        except Exception as exc:  # pragma: no cover - upstream best effort
            logger.debug("ai search set resolve failed phrase=%r: %s", phrase, exc)
            return None

    resolved = await asyncio.gather(*(one(p) for p in phrases))
    return [r for r in resolved if r and r.get("id")]


async def _set_page(resolved: dict[str, Any], game: str | None) -> list[dict[str, Any]]:
    """One page of cards FROM a resolved set — ground truth for set asks."""
    from app.services.catalog import catalog_browse_service

    try:
        set_game = str(resolved.get("tcg") or game or "pokemon")
        body = await catalog_browse_service.browse_catalog(
            set_game, page=1, page_size=PER_SET, set_id=str(resolved["id"])
        )
    except Exception as exc:  # pragma: no cover - upstream best effort
        logger.debug("ai search set page failed set=%r: %s", resolved.get("name"), exc)
        return []
    cards = body.get("cards")  # browse pages say "cards", search says "results"
    return list(cards) if isinstance(cards, list) else []


def _prefer_sets(
    cards: list[dict[str, Any]], set_names: list[str]
) -> list[dict[str, Any]]:
    """Stable partition: cards printed in the asked-for sets come first.

    For "base set charizard" the Base Set printing must lead — other
    printings stay available below, never dropped."""
    wanted = [_norm_name(n) for n in set_names if n]
    if not wanted:
        return cards

    def hits(card: dict[str, Any]) -> bool:
        s = _norm_name(str(card.get("set_name") or ""))
        return bool(s) and any(w in s or s in w for w in wanted)

    front = [c for c in cards if hits(c)]
    back = [c for c in cards if not hits(c)]
    return front + back


def _interleave(
    per_candidate: list[list[dict[str, Any]]], limit: int
) -> list[dict[str, Any]]:
    """Round-robin merge, preserving candidate rank, deduped by card id."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    depth = max((len(cards) for cards in per_candidate), default=0)
    for tier in range(depth):
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
    if not await health.available():
        return None  # cooling down / unconfigured — router serves the fallback
    plan, cached = await _plan_for(q, game_hint)
    if plan is None:
        return None
    lookup_game = plan.game or game_hint

    # Retrieval: name lookups per candidate, PLUS — when the ask names
    # sets — pages from the REAL resolved sets. Set pages are ground truth
    # for "movie promos"-style asks, so they lead the interleave; for a
    # single-card ask ("base set charizard") the asked-for printing leads.
    collection = plan.intent == "collection"
    resolved_sets = await _resolve_sets(plan.sets, lookup_game) if plan.sets else []
    set_shelves: list[list[dict[str, Any]]] = []
    if collection and resolved_sets:
        set_shelves = [
            shelf
            for shelf in await asyncio.gather(
                *(_set_page(r, lookup_game) for r in resolved_sets)
            )
            if shelf
        ]
    per_candidate = await asyncio.gather(
        *(_cards_for(name, lookup_game) for name in plan.candidates)
    )
    columns = set_shelves + list(per_candidate)
    review_worthy = collection or bool(resolved_sets)
    pooled = _interleave(columns, max(limit, VERIFY_POOL) if review_worthy else limit)
    if not collection and resolved_sets:
        pooled = _prefer_sets(pooled, [str(r.get("name")) for r in resolved_sets])

    # Set-flavored shelves get the review pass: the model sees the JSON of
    # what we're about to show and drops what doesn't belong.
    verified = False
    if review_worthy:
        pooled, verified = await verify.review_shelf(q, lookup_game, pooled)

    results = pooled[:limit]
    return {
        "query": q,
        "message": plan.message,
        "candidates": plan.candidates,
        "game": lookup_game,
        "intent": plan.intent,
        "results": results,
        "total": len(results),
        "source": "ai",
        "cached": cached,
        "verified": verified,
    }


__all__ = ["ai_search"]
