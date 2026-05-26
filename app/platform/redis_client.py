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


class _InMemoryRedis:
    """Tiny in-process stand-in used when Redis isn't available."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self._lock = asyncio.Lock()

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

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        async with self._lock:
            expires_at = time.time() + ex if ex else None
            self._store[key] = (value, expires_at)
            return True

    async def setex(self, key: str, ex: int, value: str) -> bool:
        return await self.set(key, value, ex=ex)

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
_init_lock = asyncio.Lock()


async def get_redis() -> Any:
    """Return a process-wide async Redis client (real or in-memory)."""
    global _client
    if _client is not None:
        return _client
    async with _init_lock:
        if _client is not None:
            return _client
        s = get_settings()
        if aioredis is None:
            logger.warning("redis package unavailable; using in-process cache")
            _client = _InMemoryRedis()
            return _client
        try:
            candidate = aioredis.from_url(s.redis_url, decode_responses=True)
            await asyncio.wait_for(candidate.ping(), timeout=2.0)
            _client = candidate
            logger.info("Connected to Redis at %s", s.redis_url)
        except Exception as exc:  # pragma: no cover - depends on env
            logger.warning("Redis unavailable (%s); using in-process cache", exc)
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
