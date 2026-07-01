"""Async Redis client with graceful in-process fallback.

If ``REDIS_URL`` points to a server we can reach, returns a real
:class:`redis.asyncio.Redis` instance.  Otherwise returns an in-process
adapter that satisfies the small subset of the API the rest of the codebase
uses (``get``, ``set``, ``setex``, ``delete``, ``publish``, ``close``).
This keeps tests + offline dev environments fully functional.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("clients.redis")

try:  # pragma: no cover - import-time guard
    import redis.asyncio as aioredis
except Exception:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]


class _NullAsyncLock:
    """No-op async lock. The in-process stub's critical sections never ``await``
    (pure dict ops), so single-threaded asyncio can't interleave them — a real
    ``asyncio.Lock`` is unnecessary AND harmful here: it binds to the loop it's
    created on and a process-wide singleton then breaks across event loops
    (e.g. pytest-asyncio's per-test loops)."""

    async def __aenter__(self) -> _NullAsyncLock:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _InMemoryRedis:
    """Tiny in-process stand-in used when Redis isn't available."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self._lock = _NullAsyncLock()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            row = self._store.get(key)
            if row is None:
                return None
            value, expires_at = row
            if expires_at is not None and time.time() > expires_at:
                self._store.pop(key, None)
                return None
            return value

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        async with self._lock:
            if nx:
                row = self._store.get(key)
                if row is not None:
                    _, expires_at = row
                    if expires_at is None or time.time() <= expires_at:
                        return None  # key live — NX fails, mirrors real Redis
            expires_at = time.time() + ex if ex else None
            self._store[key] = (value, expires_at)
            return True

    async def setex(self, key: str, ex: int, value: str) -> bool:
        await self.set(key, value, ex=ex)
        return True

    async def incrby(self, key: str, amount: int = 1) -> int:
        async with self._lock:
            row = self._store.get(key)
            current = 0
            if row is not None:
                value, expires_at = row
                if expires_at is None or time.time() <= expires_at:
                    try:
                        current = int(value)
                    except (TypeError, ValueError):
                        current = 0
                expires = expires_at
            else:
                expires = None
            current += amount
            self._store[key] = (str(current), expires)
            return current

    async def incr(self, key: str) -> int:
        return await self.incrby(key, 1)

    async def expire(self, key: str, seconds: int) -> bool:
        async with self._lock:
            row = self._store.get(key)
            if row is None:
                return False
            value, _ = row
            self._store[key] = (value, time.time() + seconds)
            return True

    async def ttl(self, key: str) -> int:
        async with self._lock:
            row = self._store.get(key)
            if row is None:
                return -2
            _, expires_at = row
            if expires_at is None:
                return -1
            return max(0, int(expires_at - time.time()))

    async def delete(self, *keys: str) -> int:
        async with self._lock:
            n = 0
            for k in keys:
                if k in self._store:
                    self._store.pop(k, None)
                    n += 1
            return n

    async def publish(self, channel: str, message: str) -> int:
        return 0

    async def close(self) -> None:
        self._store.clear()

    async def ping(self) -> bool:
        return True


_client: Any | None = None


async def get_redis() -> Any:
    """Return a process-wide async Redis client (real or in-memory).

    No init lock (a real ``asyncio.Lock`` breaks across event loops, e.g. under
    pytest-asyncio). Instead, every assignment after an ``await`` is guarded by
    ``if _client is None`` — so two concurrent cold callers converge on ONE
    client (the first to finish wins; the loser's probe just leaks harmlessly)
    rather than each creating a separate in-memory store.
    """
    global _client
    if _client is not None:
        return _client
    s = get_settings()
    if aioredis is None:
        logger.warning("redis package unavailable; using in-process cache")
        if _client is None:
            _client = _InMemoryRedis()
        return _client
    try:
        candidate = aioredis.from_url(s.redis_url, decode_responses=True)
        await asyncio.wait_for(candidate.ping(), timeout=2.0)
        if _client is None:  # nobody else won the race during our probe
            _client = candidate
            logger.info("Connected to Redis at %s", s.redis_url)
    except Exception as exc:  # pragma: no cover - depends on env
        logger.warning("Redis unavailable (%s); using in-process cache", exc)
        if _client is None:
            _client = _InMemoryRedis()
        return _client


async def close_redis() -> None:
    """Close the shared Redis client (called from app shutdown)."""
    global _client
    if _client is None:
        return
    try:
        await _client.close()
    except Exception as exc:  # pragma: no cover
        logger.debug("Error closing redis client: %s", exc)
    _client = None


__all__ = ["close_redis", "get_redis"]
