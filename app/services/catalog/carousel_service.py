"""AI-generated marketplace carousels.

Asks a model (OpenAI by default — `OPENAI_API_KEY`/`carousel_model`; falls back
to Anthropic if only that's configured) to *design* a fresh set of themed
shelves for a game — the theme, the copy, and a constrained filter recipe —
which the web then renders through the normal rail engine. Crucially the model
never sees or returns card data: it only emits carousel *definitions* over a
fixed vocabulary, so one cheap call a day produces creative merchandising while
the actual cards keep coming from our cached catalog. Results are cached per
game per day; when no model is configured (or it errors) the endpoint returns
the **curated registry** (``carousel_registry`` — checked-in JSON + live
operator overrides) — the single source of truth both the web and mobile
render, so the clients no longer each hardcode their own.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Iterable
from datetime import date
from typing import Any

from pydantic import ValidationError

from app.config import get_settings
from app.platform.redis_client import get_redis
from app.schemas.carousel import (
    CarouselRecipe,
    CarouselResponse,
    ResolvedCarousels,
    ResolvedRail,
)
from app.services.catalog import carousel_registry
from app.services.catalog.carousel_registry import GAME_LABELS
from app.utils.logger import get_logger

logger = get_logger("services.carousel")

# ──────────────────────────────────────────────────────────────────────────
# Curated shelf pool — the canonical, always-on merchandising the endpoint
# serves; the SINGLE SOURCE OF TRUTH both the web and mobile render. It now
# lives in ``carousel_registry`` (checked-in JSON + live operator overrides
# from /admin/carousels), and is re-read on every serve so an operator edit
# shows up without a deploy. The AI layer below only *adds* to / replaces it
# when a model is configured. Titles keep the ``{label}`` placeholder — each
# client interpolates it with its own game label.
# ──────────────────────────────────────────────────────────────────────────


_AI_TTL = 24 * 60 * 60  # AI result is good for a day
_COUNT = 6  # shelves to generate

_SYSTEM = """You are a trading-card marketplace merchandiser designing the \
discovery carousels ("shelves") for a {label} singles storefront.

Return ONLY a JSON object of the form {{"shelves": [ ...exactly {count} shelf \
objects... ]}} — no prose, no code fences. Each shelf object has this exact shape:
{{
  "id": "kebab-case-slug",
  "title": "punchy, <= 48 chars",
  "subtitle": "one line, <= 110 chars",
  "source": "value" | "catalog",
  "priceMin": number (USD, optional),
  "priceMax": number (USD, optional),
  "rarityPattern": "lowercase regex alternation, optional, e.g. secret|rainbow|illustration",
  "sort": "price_desc" | "price_asc" | "name",
  "limit": 20
}}

Rules:
- "source":"value" for price/rarity-themed shelves (the priced market pool);
  "source":"catalog" for a general "explore" shelf with no price filter — \
use it for AT MOST ONE shelf; every other shelf must be "value" or \
"trending" so it renders real priced inventory.
- Theme each shelf with a price band (priceMin/priceMax) AND/OR a rarityPattern.
- Make the {count} shelves DIVERSE: mix value tiers (grails, premium, mid, \
budget), rarity themes, and chase pulls. Titles should feel specific to \
{label} collecting culture.
- Keep it tasteful and accurate; do not invent prices or card names."""


def configured() -> bool:
    s = get_settings()
    return bool(s.openai_api_key or s.anthropic_api_key)


#: Bump when the curated pool or recipe shape changes so a deploy invalidates
#: any still-cached placeholders instead of serving them until TTL.
_CACHE_VERSION = "v3"


def _cache_key(game: str) -> str:
    return f"loupe:carousels:{_CACHE_VERSION}:{game}:{date.today().isoformat()}"


async def _cache_get(game: str) -> CarouselResponse | None:
    try:
        raw = await (await get_redis()).get(_cache_key(game))
        if raw:
            return CarouselResponse.model_validate_json(raw)
    except Exception as exc:  # pragma: no cover - cache best effort
        logger.debug("carousel cache_get failed: %s", exc)
    return None


async def _cache_set(resp: CarouselResponse, ttl: int) -> None:
    try:
        await (await get_redis()).setex(
            _cache_key(resp.game), ttl, resp.model_dump_json()
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("carousel cache_set failed: %s", exc)


def _coerce(raw: list[dict[str, object]]) -> list[CarouselRecipe]:
    """Validate model output into recipes, dropping anything malformed."""
    out: list[CarouselRecipe] = []
    seen: set[str] = set()
    for item in raw:
        try:
            recipe = CarouselRecipe.model_validate(item)
        except ValidationError:
            continue
        if recipe.id in seen:
            continue
        seen.add(recipe.id)
        out.append(recipe)
    return out


def _parse_recipes(text: str) -> list[CarouselRecipe]:
    """Pull the shelf array out of a model response (object-wrapped or bare),
    tolerant of stray prose/code fences, then validate each recipe."""
    text = text.strip()
    data: object = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Tolerate code fences / prose: grab the array first (a bare list or the
        # inner "shelves" array), else fall back to an object wrapper.
        for open_c, close_c in (("[", "]"), ("{", "}")):
            i, j = text.find(open_c), text.rfind(close_c)
            if i != -1 and j != -1:
                try:
                    data = json.loads(text[i : j + 1])
                    break
                except json.JSONDecodeError:
                    continue
    if isinstance(data, dict):
        for key in ("shelves", "carousels", "data", "items"):
            inner = data.get(key)
            if isinstance(inner, list):
                data = inner
                break
    if not isinstance(data, list):
        return []
    return _coerce(data)


async def _ask_openai(label: str) -> str:
    from openai import AsyncOpenAI

    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.chat.completions.create(
        model=settings.carousel_model,
        messages=[
            {"role": "system", "content": _SYSTEM.format(label=label, count=_COUNT)},
            {"role": "user", "content": f"Design the shelves for {label}."},
        ],
        response_format={"type": "json_object"},
        temperature=0.85,  # creative variety between days
        max_tokens=1500,
    )
    return resp.choices[0].message.content or ""


async def _ask_anthropic(label: str) -> str:
    from anthropic import AsyncAnthropic

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=settings.nl_query_model,
        max_tokens=1500,
        system=_SYSTEM.format(label=label, count=_COUNT),
        messages=[{"role": "user", "content": f"Design the shelves for {label}."}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


async def _generate_ai(game: str, label: str) -> list[CarouselRecipe]:
    """Generate shelves via whichever model is configured (OpenAI preferred)."""
    settings = get_settings()
    if settings.openai_api_key:
        text = await _ask_openai(label)
    elif settings.anthropic_api_key:
        text = await _ask_anthropic(label)
    else:
        return []
    return _parse_recipes(text)


#: Background generation tasks (held so they aren't GC'd) + a per-instance
#: in-flight guard so concurrent misses don't each spawn a model call.
_bg_tasks: set[asyncio.Task[None]] = set()
_inflight: set[str] = set()
#: Last generation-attempt time per game (monotonic). After an attempt we back
#: off for a cooldown so a broken/quota-less key isn't retried on every request.
_last_attempt: dict[str, float] = {}
_RETRY_COOLDOWN_SECONDS = 600.0  # 10 min


def _spawn_generation(game: str, label: str) -> None:
    """Generate AI shelves in the BACKGROUND and cache them, so the request path
    never blocks on the model. Deduped per game, and rate-limited per game so a
    failing key (bad / no quota) is retried at most once per cooldown instead of
    on every marketplace load."""
    if game in _inflight:
        return
    # Use a None sentinel for "never attempted" — NOT 0.0. On a freshly booted
    # host ``time.monotonic()`` can be small (it counts from boot), so
    # ``monotonic() - 0.0 < cooldown`` would wrongly suppress the very first
    # attempt for the first ~10 min of an instance's life. Only cool down a
    # game that has an actual recorded prior attempt.
    last = _last_attempt.get(game)
    if last is not None and time.monotonic() - last < _RETRY_COOLDOWN_SECONDS:
        return
    _inflight.add(game)
    _last_attempt[game] = time.monotonic()

    async def _run() -> None:
        try:
            recipes = await _generate_ai(game, label)
            if recipes:
                await _cache_set(
                    CarouselResponse(game=game, source="ai", carousels=recipes),
                    _AI_TTL,
                )
        except Exception as exc:  # pragma: no cover - model/network best effort
            logger.warning("carousel AI generation failed for %s: %s", game, exc)
        finally:
            _inflight.discard(game)

    task = asyncio.create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def get_carousels(game: str) -> CarouselResponse:
    """Return a game's shelves WITHOUT ever blocking the request on the model.

    The curated set — the checked-in registry merged with the operator's live
    overrides (``carousel_registry.recipes_for``) — is recomputed on every call
    so a portal edit shows up immediately, and it's what serves whenever no AI
    design is cached (kicking generation in the background so the *next* load
    serves the AI version). The operator's AI kill switch pins serving to the
    curated registry and stops model calls entirely.
    """
    label = GAME_LABELS.get(game, game.title())
    overrides = await carousel_registry.get_overrides()
    if overrides.aiEnabled:
        cached = await _cache_get(game)
        if cached is not None and cached.source == "ai":
            return cached
        if configured():
            _spawn_generation(game, label)
    return CarouselResponse(
        game=game,
        source="curated",
        carousels=carousel_registry.recipes_for(game, overrides),
    )


# ──────────────────────────────────────────────────────────────────────────
# Resolved carousels — compile each recipe into ACTUAL CARDS server-side.
#
# The recipe endpoint above leaves the compilation (price band / rarity / sort /
# limit → cards) to each client, which drifts: a recipe that can't be filled
# from the thin priced shelf shows an empty rail on one client and self-hides on
# another. Resolving here makes the backend the single source of truth — it runs
# the recipe against the shelf/catalog, DROPS any rail too thin to show, and
# returns ready-to-render rails so web and mobile paint the exact same carousels.
# ──────────────────────────────────────────────────────────────────────────

_RESOLVED_TTL = 15 * 60  # resolved payload is heavier to build; prices drift slowly
_MIN_RAIL = 4  # a rail with fewer cards than this is dropped (matches client minItems)


def _interp(text: str, label: str) -> str:
    return text.replace("{label}", label)


def _price_of(card: dict[str, Any]) -> float | None:
    ps = card.get("pricing_summary")
    if isinstance(ps, dict):
        market = ps.get("market")
        if isinstance(market, dict) and market.get("amount") is not None:
            try:
                return float(market["amount"])
            except (TypeError, ValueError):
                return None
    return None


def _apply_lens(
    cards: list[dict[str, Any]], recipe: CarouselRecipe
) -> list[dict[str, Any]]:
    """Server-side mirror of the web `cardFilters` lens: price band, rarity
    pattern, sort, limit — applied to a fetched shelf."""
    out = list(cards)

    lo, hi = recipe.priceMin, recipe.priceMax
    if lo is not None or hi is not None:
        kept: list[dict[str, Any]] = []
        for c in out:
            p = _price_of(c)
            if p is None:
                continue  # a price-band rail only makes sense for priced cards
            if lo is not None and p < lo:
                continue
            if hi is not None and p > hi:
                continue
            kept.append(c)
        out = kept

    if recipe.rarityPattern:
        try:
            rx = re.compile(recipe.rarityPattern, re.IGNORECASE)
            out = [c for c in out if c.get("rarity") and rx.search(str(c["rarity"]))]
        except re.error:
            pass  # tolerate a bad pattern rather than drop the whole rail

    sort = recipe.sort or "price_desc"
    if sort in ("price_desc", "price_asc"):
        out.sort(key=lambda c: _price_of(c) or 0.0, reverse=(sort == "price_desc"))
    elif sort == "name":
        out.sort(key=lambda c: str(c.get("name") or ""))

    return out[: (recipe.limit or 20)]


async def _shelf_cards(
    game: str, sort: str, max_price: float | None = None, limit: int = 48
) -> list[dict[str, Any]]:
    from app.services.market import trending_service

    body = await trending_service.get_shelf(
        tcg=game, sort=sort, max_price=max_price, limit=limit
    )
    cards = body.get("cards")
    return list(cards) if isinstance(cards, list) else []


async def _catalog_cards(game: str, limit: int = 24) -> list[dict[str, Any]]:
    """A varied slice of the game's catalog.

    Page 1 sorted by name is alphabetically FIRST cards — every catalog
    shelf led with the same Abras. Fetch a wide pool instead and rotate a
    date-seeded sample so catalog rails differ per day AND per shelf size,
    while staying deterministic within the day (stable cache).
    """
    from app.services.catalog import catalog_browse_service

    pool_size = max(limit * 4, 96)
    body = await catalog_browse_service.browse_catalog(
        game=game, page=1, page_size=pool_size, sort="name"
    )
    cards = body.get("cards")
    pool = list(cards) if isinstance(cards, list) else []
    rng = random.Random(f"{game}:{date.today().isoformat()}")
    rng.shuffle(pool)
    return pool[:limit]


async def _resolve_recipe(game: str, recipe: CarouselRecipe) -> list[dict[str, Any]]:
    if recipe.source == "catalog":
        # Fetch wide, then apply the SAME lens as priced shelves — a catalog
        # rail themed "holo rares" must actually filter to holo rares, not
        # serve the raw browse page under a fancy title.
        pool = await _catalog_cards(game, limit=max((recipe.limit or 20) * 3, 48))
        return _apply_lens(pool, recipe)
    sort = "trending" if recipe.source == "trending" else "value"
    cards = await _shelf_cards(game, sort=sort, max_price=recipe.priceMax, limit=48)
    return _apply_lens(cards, recipe)


def _resolved_cache_key(game: str) -> str:
    return (
        f"loupe:carousels:resolved:{_CACHE_VERSION}:{game}:{date.today().isoformat()}"
    )


async def _resolved_cache_get(game: str) -> ResolvedCarousels | None:
    try:
        raw = await (await get_redis()).get(_resolved_cache_key(game))
        if raw:
            return ResolvedCarousels.model_validate_json(raw)
    except Exception as exc:  # pragma: no cover - cache best effort
        logger.debug("resolved carousel cache_get failed: %s", exc)
    return None


async def _resolved_cache_set(resolved: ResolvedCarousels) -> None:
    try:
        await (await get_redis()).setex(
            _resolved_cache_key(resolved.game),
            _RESOLVED_TTL,
            resolved.model_dump_json(),
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("resolved carousel cache_set failed: %s", exc)


async def resolve_carousels(game: str) -> ResolvedCarousels:
    """Resolve a game's recipe pool into ready-to-render rails (with cards).

    Builds a momentum anchor + the curated/AI recipe rails + a catalog "explore"
    rail, resolving each to real cards and DROPPING any rail below ``_MIN_RAIL``
    so a client never renders an empty shelf. This is the single source of truth
    both web and mobile render — identical carousels, zero client-side filtering.
    """
    game = (game or "").lower()
    cached = await _resolved_cache_get(game)
    if cached is not None:
        return cached

    label = GAME_LABELS.get(game, game.title())
    base = await get_carousels(game)
    rails: list[ResolvedRail] = []
    seen: set[str] = set()

    # 1. Momentum anchor — real trending; dropped when the feed is thin/empty.
    anchor = await _shelf_cards(game, sort="trending", limit=24)
    if len(anchor) >= _MIN_RAIL:
        rails.append(
            ResolvedRail(
                id="trending",
                kind="cards",
                title=f"Trending in {label}",
                subtitle=f"The {label} cards moving most across marketplaces.",
                cards=anchor,
            )
        )
        seen.add("trending")

    # 2. The curated/AI recipe pool, resolved and de-duped by id.
    # Cards already placed on an earlier rail — a card must appear on at
    # most ONE shelf per page, or themed rails read as copy-paste filler.
    placed: set[str] = {str(c.get("id")) for c in anchor if c.get("id")}

    for recipe in base.carousels:
        if recipe.id in seen:
            continue
        seen.add(recipe.id)
        cards = await _resolve_recipe(game, recipe)
        cards = [c for c in cards if str(c.get("id")) not in placed]
        if len(cards) >= (recipe.minItems or _MIN_RAIL):
            placed.update(str(c.get("id")) for c in cards if c.get("id"))
            rails.append(
                ResolvedRail(
                    id=recipe.id,
                    kind="catalog" if recipe.source == "catalog" else "cards",
                    title=_interp(recipe.title, label),
                    subtitle=_interp(recipe.subtitle, label),
                    cards=cards,
                )
            )

    # 3. Explore anchor — a broad catalog rail that fills for every game
    #    (including catalog-only ones whose priced pool is empty).
    if "explore" not in seen:
        explore = await _catalog_cards(game, limit=48)
        explore = [c for c in explore if str(c.get("id")) not in placed][:24]
        if len(explore) >= _MIN_RAIL:
            rails.append(
                ResolvedRail(
                    id="explore",
                    kind="catalog",
                    title=f"Explore {label}",
                    subtitle=f"A look across the {label} catalog — open any card for details.",
                    cards=explore,
                )
            )

    resolved = ResolvedCarousels(game=game, source=base.source, rails=rails)
    await _resolved_cache_set(resolved)
    return resolved


# ──────────────────────────────────────────────────────────────────────────
# Admin helpers — what /v1/admin/carousels drives.
# ──────────────────────────────────────────────────────────────────────────


async def cached_ai(game: str) -> CarouselResponse | None:
    """Today's cached AI design for a game, if one exists."""
    cached = await _cache_get(game)
    return cached if cached is not None and cached.source == "ai" else None


async def cached_resolved(game: str) -> ResolvedCarousels | None:
    """The cached resolved payload (what clients render), if warm."""
    return await _resolved_cache_get(game)


async def purge_resolved(games: Iterable[str] | None = None) -> None:
    """Drop cached resolved payloads so an operator edit serves immediately.

    The AI recipe cache is deliberately left alone — registry edits don't
    invalidate an AI design, and regeneration overwrites it explicitly.
    """
    try:
        redis = await get_redis()
        for game in games if games is not None else GAME_LABELS:
            await redis.delete(_resolved_cache_key(game))
    except Exception as exc:  # pragma: no cover - cache best effort
        logger.debug("carousel purge_resolved failed: %s", exc)


async def regenerate_ai(game: str) -> CarouselResponse:
    """Force a fresh AI design pass NOW (admin regenerate button).

    Unlike the background path this runs synchronously — the operator is
    watching — and it overwrites today's cached design + purges the game's
    resolved payload so the new shelves serve on the next load. Raises
    ``RuntimeError`` when the model yields nothing usable.
    """
    label = GAME_LABELS.get(game, game.title())
    recipes = await _generate_ai(game, label)
    if not recipes:
        raise RuntimeError("the model returned no usable shelves")
    resp = CarouselResponse(game=game, source="ai", carousels=recipes)
    await _cache_set(resp, _AI_TTL)
    await purge_resolved([game])
    return resp
