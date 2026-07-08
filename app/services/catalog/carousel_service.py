"""AI-generated marketplace carousels.

Asks a model (OpenAI by default — `OPENAI_API_KEY`/`carousel_model`; falls back
to Anthropic if only that's configured) to *design* a fresh set of themed
shelves for a game — the theme, the copy, and a constrained filter recipe —
which the web then renders through the normal rail engine. Crucially the model
never sees or returns card data: it only emits carousel *definitions* over a
fixed vocabulary, so one cheap call a day produces creative merchandising while
the actual cards keep coming from our cached catalog. Results are cached per
game per day; when no model is configured (or it errors) the endpoint returns
the built-in **curated pool** (``_curated_for``) — the single source of truth
both the web and mobile render, so the clients no longer each hardcode their own.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date

from pydantic import ValidationError

from app.config import get_settings
from app.platform.redis_client import get_redis
from app.schemas.carousel import CarouselRecipe, CarouselResponse
from app.utils.logger import get_logger

logger = get_logger("services.carousel")

GAME_LABELS: dict[str, str] = {
    "pokemon": "Pokémon",
    "magic": "Magic: The Gathering",
    "yugioh": "Yu-Gi-Oh!",
    "onepiece": "One Piece",
    "digimon": "Digimon",
}

# ──────────────────────────────────────────────────────────────────────────
# Curated shelf pool — the canonical, always-on merchandising the endpoint
# serves. This is the SINGLE SOURCE OF TRUTH both the web and mobile render
# (they used to each hardcode their own copy). The AI layer below only *adds*
# to / replaces this when a model is configured; with no model, this pool is
# what ships. Titles keep the ``{label}`` placeholder — each client
# interpolates it with its own game label (web "Magic" vs long forms, etc.),
# exactly as the web strategy pool did before it moved here.
# ──────────────────────────────────────────────────────────────────────────
_CURATED_POOL: list[CarouselRecipe] = [
    CarouselRecipe(
        id="grails",
        title="Grails & chase cards",
        subtitle="The trophy {label} cards serious collectors hunt.",
        source="value",
        priceMin=250,
        sort="price_desc",
    ),
    CarouselRecipe(
        id="rainbow",
        title="Rainbow & secret rares",
        subtitle="The flashiest pulls in {label}.",
        source="value",
        rarityPattern="secret|rainbow|illustration|special",
        sort="price_desc",
    ),
    CarouselRecipe(
        id="premium",
        title="Premium tier · $150+",
        subtitle="Heavy hitters at live market value.",
        source="value",
        priceMin=150,
        sort="price_desc",
    ),
    CarouselRecipe(
        id="midrange",
        title="Collector picks · $25–$150",
        subtitle="Standouts that won't break the bank.",
        source="value",
        priceMin=25,
        priceMax=150,
        sort="price_desc",
    ),
    CarouselRecipe(
        id="holo-hits",
        title="Holo & ultra-rare hits",
        subtitle="Shiny {label} cards worth chasing.",
        source="value",
        rarityPattern="holo|ultra|ex|gx|\\bv\\b|vmax|vstar",
        sort="price_desc",
    ),
    CarouselRecipe(
        id="under25",
        title="Great finds under $25",
        subtitle="Quality {label} pickups, priced live.",
        source="value",
        priceMax=25,
        sort="price_desc",
    ),
    CarouselRecipe(
        id="steals5",
        title="Steals under $5",
        subtitle="Affordable {label} singles.",
        source="value",
        priceMax=5,
        sort="price_desc",
    ),
    CarouselRecipe(
        id="budget-binder",
        title="Budget binder fillers",
        subtitle="Round out the collection for less.",
        source="value",
        priceMax=10,
        sort="price_asc",
    ),
    CarouselRecipe(
        id="blue-chips",
        title="Blue-chip singles",
        subtitle="Established, high-value {label} cards.",
        source="value",
        priceMin=75,
        priceMax=400,
        sort="price_desc",
    ),
]

#: Games whose catalog has no price feed yet (mirrors
#: ``trending_service._CATALOG_ONLY_SOURCE``). Value/price shelves are
#: meaningless for them, and the ``catalog`` rail kind can't filter by rarity,
#: so a "themed" shelf would just reorder the same cards — the exact
#: same-cards-in-every-carousel trap. They therefore get NO curated pool and
#: lean on the clients' structural anchors (explore/sets/sealed). See
#: ``[[supported-tcgs]]`` — add prices to light up their value shelves.
_CATALOG_ONLY_GAMES: frozenset[str] = frozenset({"onepiece", "digimon"})
#: Games that actually have a priced discovery pool.
_PRICED_GAMES: frozenset[str] = frozenset(GAME_LABELS) - _CATALOG_ONLY_GAMES


def _curated_for(game: str) -> list[CarouselRecipe]:
    """The always-on curated shelf pool for a game.

    Priced games (Pokémon/Magic/Yu-Gi-Oh!) get the value/rarity merchandising
    pool; catalog-only or unknown games get an empty list (their structural
    anchors carry the storefront). This is what makes ``/v1/public/carousels``
    the single source of truth instead of an empty placeholder the clients had
    to paper over.
    """
    if game not in _PRICED_GAMES:
        return []
    return list(_CURATED_POOL)


_AI_TTL = 24 * 60 * 60  # AI result is good for a day
_FALLBACK_TTL = 60 * 60  # retry the model within the hour once a key is set
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
  "source":"catalog" for a general "explore" shelf with no price filter.
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
_CACHE_VERSION = "v2"


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

    A cache miss returns the curated set instantly and kicks AI generation in the
    background, so the *next* load serves the AI version. The curated set is the
    canonical merchandising pool (``_curated_for``) — the single source of truth
    both clients render — so the storefront is never bare, and a slow/at-fault
    model can never spike request latency.
    """
    label = GAME_LABELS.get(game, game.title())
    cached = await _cache_get(game)
    if cached is not None:
        # Upgrade a curated placeholder to AI in the background once a model is
        # configured (e.g. the key was just set) — still never blocking.
        if cached.source == "curated" and configured():
            _spawn_generation(game, label)
        return cached

    # Cold miss: cache the curated pool so concurrent requests don't all spawn,
    # return it immediately, and generate AI off the request path.
    curated = CarouselResponse(
        game=game, source="curated", carousels=_curated_for(game)
    )
    await _cache_set(curated, _FALLBACK_TTL)
    if configured():
        _spawn_generation(game, label)
    return curated
