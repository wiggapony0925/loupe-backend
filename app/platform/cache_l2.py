"""Durable L2 cache backed by Postgres (``kv_cache`` table).

Production Cloud Run has no reachable Redis (DEPLOY.md §2): ``get_redis()``
degrades to a per-instance in-memory dict that is wiped on every instance
recycle, so every "cached" catalog surface was really an uncached live proxy.
This module is the second tier under that L1: small JSON strings with an
expiry, stored in the database we already have — shared by all instances and
surviving restarts.

Contract: **best effort, never raises.** A cache read/write failure must never
take down a request; callers treat ``None`` as a miss. Expired rows are
ignored on read and purged opportunistically on write (~1% of writes), so the
table stays small without a scheduled vacuum job.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update

from app.utils.logger import get_logger

logger = get_logger("platform.cache_l2")

#: Refuse to store pathological values (whole-catalog blobs are ~5-10 MB;
#: anything past this is a bug, not a cache entry).
_MAX_VALUE_BYTES = 24 * 1024 * 1024

#: Probability that a write also purges expired rows.
_PURGE_PROBABILITY = 0.01


def _now() -> datetime:
    return datetime.now(UTC)


async def kv_get(key: str) -> str | None:
    """Return the cached string for *key*, or ``None`` on miss/expiry/error."""
    try:
        from app.db import get_sessionmaker
        from app.models.kv_cache import KvCacheEntry

        maker = get_sessionmaker()
        async with maker() as session:
            row = (
                await session.execute(
                    select(KvCacheEntry.value, KvCacheEntry.expires_at).where(
                        KvCacheEntry.key == key
                    )
                )
            ).first()
            if row is None:
                return None
            value, expires_at = row
            # SQLite returns naive datetimes; treat them as UTC.
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= _now():
                return None
            return value
    except Exception as exc:  # pragma: no cover - cache best effort
        logger.debug("kv_get failed key=%s: %s", key, exc)
        return None


async def kv_set(key: str, value: str, ttl_seconds: int) -> None:
    """Upsert *key* with a TTL. Best effort — failures are logged and dropped."""
    if not key or ttl_seconds <= 0:
        return
    if len(value) > _MAX_VALUE_BYTES:
        logger.warning(
            "kv_set refused oversized value key=%s bytes=%s", key, len(value)
        )
        return
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    try:
        from app.db import get_sessionmaker
        from app.models.kv_cache import KvCacheEntry

        maker = get_sessionmaker()
        async with maker() as session:
            result = await session.execute(
                update(KvCacheEntry)
                .where(KvCacheEntry.key == key)
                .values(value=value, expires_at=expires_at)
            )
            if (getattr(result, "rowcount", 0) or 0) == 0:
                session.add(KvCacheEntry(key=key, value=value, expires_at=expires_at))
            if random.random() < _PURGE_PROBABILITY:
                await session.execute(
                    delete(KvCacheEntry).where(KvCacheEntry.expires_at <= _now())
                )
            await session.commit()
    except Exception as exc:  # pragma: no cover - races / transient DB errors
        # A concurrent insert of the same key can trip the PK; the other
        # writer's value is as good as ours.
        logger.debug("kv_set failed key=%s: %s", key, exc)


async def kv_delete(key: str) -> None:
    try:
        from app.db import get_sessionmaker
        from app.models.kv_cache import KvCacheEntry

        maker = get_sessionmaker()
        async with maker() as session:
            await session.execute(delete(KvCacheEntry).where(KvCacheEntry.key == key))
            await session.commit()
    except Exception as exc:  # pragma: no cover
        logger.debug("kv_delete failed key=%s: %s", key, exc)


__all__ = ["kv_delete", "kv_get", "kv_set"]
