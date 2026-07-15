"""Shelf verification — the answer reviews itself before it ships.

For collection asks ("movie promos", "evolving skies alt arts") the noise
doesn't come from the model — it comes from RETRIEVAL: a name search for
"Ancient Mew" happily returns every plain Mew in the catalog. So before the
shelf is served, the model gets the JSON of what we're ABOUT to show
(index / name / set) next to the user's ask and returns the indexes that
truly belong, best first. Verification is far easier than recall, so it
runs on the cheap model, and a rejected/garbled review simply keeps the
original shelf — this pass can only ever REORDER or TRIM, never break.

Verified orders are kv-cached per (query, game, shelf fingerprint): the
same question over the same shelf never pays for a second review.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from app.platform.cache_l2 import kv_get, kv_set
from app.services.ai import providers
from app.services.ai.config import (
    VERIFY_CACHE_KEY,
    VERIFY_ENABLED,
    VERIFY_MAX_TOKENS,
    VERIFY_MODEL,
    VERIFY_POOL,
    VERIFY_TTL,
)
from app.utils.logger import get_logger

logger = get_logger("services.ai.verify")

_SYSTEM = """You review what a trading-card marketplace is about to show a \
collector. You get their ask and a JSON list of cards (i = index, n = name, \
s = set). Return ONLY {"keep": [indexes that truly belong, best match \
first]} — no prose. Drop cards that do not fit the ask (wrong character, \
wrong set, an unrelated card that merely shares a word). When the ask names \
a set or era, the card's SET decides. Keep every card that genuinely fits; \
if nothing clearly fits, keep the closest few rather than none."""


def _fingerprint(cards: list[dict[str, Any]]) -> str:
    ids = ",".join(str(c.get("id")) for c in cards)
    return hashlib.sha1(ids.encode()).hexdigest()[:16]


def _fold(q: str) -> str:
    return (
        unicodedata.normalize("NFKD", q).encode("ascii", "ignore").decode().lower()
    ).strip()


def _parse_keep(text: str, pool_size: int) -> list[int] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if i == -1 or j == -1:
            return None
        try:
            data = json.loads(text[i : j + 1])
        except json.JSONDecodeError:
            return None
    keep = data.get("keep") if isinstance(data, dict) else None
    if not isinstance(keep, list):
        return None
    seen: set[int] = set()
    order: list[int] = []
    for idx in keep:
        if isinstance(idx, int) and 0 <= idx < pool_size and idx not in seen:
            seen.add(idx)
            order.append(idx)
    # An explicit empty keep is a VERDICT ("none of this fits"), not a
    # failure — the caller turns it into an honest miss instead of junk.
    return order


async def review_shelf(
    query: str, game: str | None, cards: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(shelf, verified)`` — the reviewed order, or the original.

    Never raises; any FAILURE (no provider, bad JSON, model error) serves the
    un-reviewed cards unchanged. But an explicit "keep nothing" verdict DOES
    empty the shelf — showing an honest "couldn't pin that down" beats
    showing cards the reviewer just said are wrong.
    """
    if not VERIFY_ENABLED or len(cards) < 2:
        return cards, False
    pool = cards[:VERIFY_POOL]
    cache_key = (
        f"{VERIFY_CACHE_KEY}:{game or 'all'}:{_fold(query)[:100]}:{_fingerprint(pool)}"
    )
    raw = await kv_get(cache_key)
    order = _parse_keep(raw, len(pool)) if raw else None

    if order is None:
        compact = [
            {"i": i, "n": c.get("name"), "s": c.get("set_name")}
            for i, c in enumerate(pool)
        ]
        user = json.dumps({"ask": query, "cards": compact}, ensure_ascii=False)
        try:
            text = await providers.ask(
                _SYSTEM, user, model=VERIFY_MODEL, max_tokens=VERIFY_MAX_TOKENS
            )
        except Exception as exc:  # review is best-effort — never break the answer
            logger.warning("ai shelf review failed: %s", exc)
            return cards, False
        order = _parse_keep(text or "", len(pool))
        if order is None:
            return cards, False
        await kv_set(cache_key, json.dumps({"keep": order}), VERIFY_TTL)

    reviewed = [pool[i] for i in order]
    if len(reviewed) < len(pool):
        logger.info(
            "ai.search shelf review kept=%d/%d q=%r",
            len(reviewed),
            len(pool),
            query[:80],
        )
    if not reviewed:
        return [], True  # "none of this fits" → honest miss, not junk
    # Anything past the reviewed pool rides along at the back untouched.
    return reviewed + cards[len(pool) :], True


__all__ = ["review_shelf"]
