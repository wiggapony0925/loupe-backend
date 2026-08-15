"""Keep the home screen's cold caches warm.

THE PROBLEM THIS SOLVES, measured rather than assumed. Production logs over
seven days:

    GET /v1/cards/trending    1,214ms median, 7,583ms max, n=70
    GET /v1/sets              3,574ms
    GET /v1/sealed/search       738ms median

Called back to back right now, the same trending endpoint answers in 250ms. It
is not slow — it is COLD. ``TRENDING_TTL`` is 15 minutes, and a miss means three
external APIs (Pokémon TCG, Scryfall, YGOPRODeck) before the rail can render.

The caching was sized for traffic that keeps itself warm. Loupe has one real
user. Opening the app twice a day means the cache has always expired by the time
anyone arrives, so the "cached" path is the one nobody ever takes — every launch
pays the cold price, which is the 5 seconds before a collection balance appears.

More traffic would fix this on its own. Until then, a warmer is the honest
substitute: it makes the cache warm on a schedule instead of hoping a user
happens to arrive inside the window.

WHY THE LIMITS ARE SPELLED OUT. The cache key is

    loupe:cards:trending:{tcg}:{limit}:{rotation_stamp}

so `limit` is part of it. Warming limit=24 does nothing for the mobile app,
which asks for 60. Warming the wrong number is indistinguishable from not
warming at all — the endpoint stays cold and the logs look unchanged.

The interval is 10 minutes against a 15-minute TTL. Warming exactly at the TTL
would race the expiry and leave a window where the first arrival still pays;
two thirds of the TTL leaves room for a slow run without ever exposing a gap.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.services.market import trending_service
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: (tcg, sort, limit) combinations the clients actually request. Each is a
#: distinct cache key; anything not listed here stays cold.
#:
#:   limit=60  loupe-frontend home rail (marketRepository)
#:   limit=24  the endpoint default, used by web and by any caller that omits it
WARM_TARGETS: tuple[tuple[str, str, int], ...] = (
    ("all", "trending", 60),
    ("all", "trending", 24),
    ("all", "value", 24),
)


async def warm_home_caches(ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """Populate the trending shelf cache for every shape the clients ask for.

    Never raises. A warmer that can take the worker down is worse than a cold
    cache: the cache costs a slow first load, the crash costs every scheduled
    job behind it.
    """
    warmed, failed = 0, 0
    for tcg, sort, limit in WARM_TARGETS:
        try:
            await trending_service.get_shelf(tcg=tcg, sort=sort, limit=limit)
            warmed += 1
        except Exception as exc:  # pragma: no cover - defensive
            failed += 1
            logger.warning(
                "cache warm failed tcg=%s sort=%s limit=%s (%s)", tcg, sort, limit, exc
            )
        # A beat between targets so three external providers are not hit in
        # lockstep by a job whose whole purpose is to be unnoticed.
        await asyncio.sleep(0.5)

    logger.info("home cache warm: %d warmed, %d failed", warmed, failed)
    return {"warmed": warmed, "failed": failed}


__all__ = ["WARM_TARGETS", "warm_home_caches"]
