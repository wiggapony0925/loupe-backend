"""Stale-while-revalidate cache with single-flight refresh.

Built for expensive, rarely-changing upstreams — e.g. a whole game catalog
behind a metered free-tier API. The contract:

* **Serve fast, always.** A value within its *fresh* window returns immediately.
  Past fresh but within the much longer *stale* window it is *still* returned
  immediately while a refresh runs in the background — users never block on the
  upstream, and a TTL expiry never surfaces as an empty page.
* **Single-flight.** A Redis ``SET NX`` lock means only one worker across the
  whole fleet refreshes at a time, so a fresh-TTL expiry under load triggers
  ONE upstream sync instead of one per concurrent request (no thundering herd).
* **Budget-aware.** An optional ``should_refresh`` predicate (e.g. an
  :class:`~app.platform.api_budget.ApiBudget` check) can veto a *background*
  refresh, so we never blow a provider's monthly quota — the stale value keeps
  serving until the budget resets. A cold miss (no value at all) always
  refreshes; there is nothing else to serve.

This is the mechanism that lets unlimited users be served from a 1000-request/
month free tier: sync the catalog into Redis a handful of times a month, serve
every read from there.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.platform.redis_client import get_redis
from app.utils.logger import get_logger

logger = get_logger("platform.cache_swr")

Refresher = Callable[[], Awaitable[Any]]
RefreshGate = Callable[[], Awaitable[bool]]

#: Hold references to fire-and-forget refresh tasks so they aren't garbage
#: collected mid-flight (asyncio only keeps weak refs to tasks).
_bg_tasks: set[asyncio.Task[Any]] = set()


async def _safe_get(r: Any, key: str) -> str | None:
    try:
        return await r.get(key)
    except Exception as exc:  # pragma: no cover - cache best effort
        logger.debug("swr get failed key=%s: %s", key, exc)
        return None


async def _store(
    r: Any, key: str, value: Any, *, fresh_ttl: int, stale_ttl: int
) -> None:
    envelope = json.dumps(
        {"data": value, "fresh_until": time.time() + fresh_ttl}, default=str
    )
    try:
        await r.setex(key, stale_ttl, envelope)
    except Exception as exc:  # pragma: no cover
        logger.debug("swr store failed key=%s: %s", key, exc)


async def _acquire_lock(r: Any, lock_key: str, lock_ttl: int) -> bool:
    try:
        return bool(await r.set(lock_key, "1", ex=lock_ttl, nx=True))
    except Exception as exc:  # pragma: no cover
        logger.debug("swr lock failed key=%s: %s", lock_key, exc)
        return False


async def _release_lock(r: Any, lock_key: str) -> None:
    try:
        await r.delete(lock_key)
    except Exception:  # pragma: no cover
        pass


async def _lock_held(r: Any, lock_key: str) -> bool:
    """True while another caller is actively refreshing (its lock is live)."""
    try:
        return bool(await r.get(lock_key))
    except Exception:  # pragma: no cover
        return False


#: Hard ceiling on how long a cold-miss waiter blocks for the lock-holder's
#: result before refreshing itself — long enough for a slow catalog sync
#: (~5s), short enough to never hang a user request.
_COLD_WAIT_CAP_SECONDS = 15.0


async def _do_refresh(
    key: str,
    *,
    fresh_ttl: int,
    stale_ttl: int,
    refresh: Refresher,
    lock_ttl: int,
    require_lock: bool,
) -> Any | None:
    """Run ``refresh`` under the single-flight lock and store the result.

    With ``require_lock`` the call no-ops when another worker holds the lock
    (background path — someone else is already refreshing). Without it, the
    refresh runs regardless after a best-effort wait (cold-miss path — we must
    return *something*).
    """
    r = await get_redis()
    lock_key = f"{key}:lock"
    have_lock = await _acquire_lock(r, lock_key, lock_ttl)
    if not have_lock and require_lock:
        return None  # another worker owns the refresh; nothing to do here
    try:
        value = await refresh()
        await _store(r, key, value, fresh_ttl=fresh_ttl, stale_ttl=stale_ttl)
        return value
    except Exception as exc:
        logger.warning("swr refresh failed key=%s: %s", key, exc)
        return None
    finally:
        if have_lock:
            await _release_lock(r, lock_key)


def _spawn_background_refresh(
    key: str,
    *,
    fresh_ttl: int,
    stale_ttl: int,
    refresh: Refresher,
    lock_ttl: int,
    should_refresh: RefreshGate | None,
) -> None:
    async def _runner() -> None:
        try:
            if should_refresh is not None and not await should_refresh():
                logger.debug("swr background refresh vetoed (budget) key=%s", key)
                return
            await _do_refresh(
                key,
                fresh_ttl=fresh_ttl,
                stale_ttl=stale_ttl,
                refresh=refresh,
                lock_ttl=lock_ttl,
                require_lock=True,
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("swr background runner error key=%s: %s", key, exc)

    task = asyncio.create_task(_runner())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def swr_get_or_refresh(
    key: str,
    *,
    fresh_ttl: int,
    stale_ttl: int,
    refresh: Refresher,
    lock_ttl: int = 60,
    should_refresh: RefreshGate | None = None,
    miss_wait_seconds: float = 2.0,
) -> Any:
    """Return a cached value, refreshing per the stale-while-revalidate policy.

    Parameters
    ----------
    key:
        Redis key for the cached value (a ``:lock`` sibling is used internally).
    fresh_ttl:
        Seconds the value is considered fresh (served with no refresh).
    stale_ttl:
        Seconds the value is *retained* and may be served stale while a refresh
        runs. Must be ``>= fresh_ttl``; make it much larger for static data.
    refresh:
        Async callable that produces the fresh value.
    should_refresh:
        Optional async gate for *background* (stale) refreshes — return ``False``
        to skip (e.g. monthly budget exhausted). Cold misses ignore it.
    miss_wait_seconds:
        On a cold miss when another worker holds the refresh lock, how long to
        wait for their result before refreshing ourselves.
    """
    r = await get_redis()
    raw = await _safe_get(r, key)
    now = time.time()

    if raw is not None:
        try:
            env = json.loads(raw)
            data = env.get("data")
            fresh_until = float(env.get("fresh_until", 0))
        except (ValueError, TypeError, AttributeError):
            data, fresh_until = None, 0.0
        if data is not None:
            if now < fresh_until:
                return data  # fresh — fast path
            # Stale: serve immediately, refresh in the background (budget-gated).
            _spawn_background_refresh(
                key,
                fresh_ttl=fresh_ttl,
                stale_ttl=stale_ttl,
                refresh=refresh,
                lock_ttl=lock_ttl,
                should_refresh=should_refresh,
            )
            return data

    # Cold miss — must produce a value. Single-flight: if another worker is
    # already refreshing, wait briefly for their result before doing it ourselves.
    value = await _do_refresh(
        key,
        fresh_ttl=fresh_ttl,
        stale_ttl=stale_ttl,
        refresh=refresh,
        lock_ttl=lock_ttl,
        require_lock=True,
    )
    if value is not None:
        return value

    # Another caller holds the lock and is refreshing. Wait for its result while
    # the lock stays live (it's still working) — NOT just a fixed window — so a
    # slow sync (e.g. a cold catalog sync) is run ONCE and every concurrent cold
    # request gets the same result instead of each starting its own. Bounded by a
    # hard cap so a request never hangs.
    lock_key = f"{key}:lock"
    hard_deadline = time.time() + max(miss_wait_seconds, _COLD_WAIT_CAP_SECONDS)
    while time.time() < hard_deadline:
        await asyncio.sleep(0.2)
        raw = await _safe_get(r, key)
        if raw is not None:
            try:
                env = json.loads(raw)
                if env.get("data") is not None:
                    return env["data"]
            except (ValueError, TypeError, AttributeError):
                break
        if not await _lock_held(r, lock_key):
            break  # holder finished without producing a value (it failed)

    # Lock holder failed or none existed — refresh ourselves as a last resort.
    return await _do_refresh(
        key,
        fresh_ttl=fresh_ttl,
        stale_ttl=stale_ttl,
        refresh=refresh,
        lock_ttl=lock_ttl,
        require_lock=False,
    )


__all__ = ["swr_get_or_refresh"]
