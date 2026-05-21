"""Lightweight per-IP sliding-window rate limiter.

This is intentionally a small, dependency-free in-process limiter rather
than slowapi or a Redis token bucket. It exists to guard the costly
fan-out endpoints (e.g. ``POST /v1/cards/resolve``) from accidental or
casual abuse — a single misbehaving client looping the endpoint can
amplify into 9× upstream provider calls each.

Caveats (read before relying on this for security):

* **Per-process state.** Cloud Run scales horizontally; an attacker
  distributed across instances will get N×limit. The right long-term
  fix is Redis-backed counters or an edge WAF rule. This limiter is
  the cheap first line of defense, not the last.
* **Per-IP only.** Behind a CDN or NAT, many users may share an IP.
  Limits are intentionally generous to avoid false positives.
* **Memory-bounded.** We cap the tracked-IP table at 10k entries and
  evict the oldest when full, so a high-cardinality attack can't OOM
  the pod.
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

from fastapi import HTTPException, Request, status


class _SlidingWindow:
    """Per-key sliding window counter."""

    __slots__ = ("_buckets", "_lock", "limit", "window_s")

    def __init__(self, *, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        # Map of key (e.g. client IP) → deque of request timestamps.
        # Bounded eviction below keeps this from growing unboundedly.
        self._buckets: dict[str, deque[float]] = {}
        self._lock = Lock()

    def hit(self, key: str) -> bool:
        """Record a hit for ``key``. Returns False when over limit."""
        now = time.monotonic()
        cutoff = now - self.window_s
        with self._lock:
            # Evict oldest tracked IPs once the table gets large so a
            # high-cardinality flood can't exhaust memory.
            if len(self._buckets) > 10_000:
                # Drop ~10% of oldest keys (those whose newest hit is
                # furthest in the past).
                victims = sorted(
                    self._buckets.items(),
                    key=lambda kv: kv[1][-1] if kv[1] else 0,
                )[:1000]
                for v_key, _ in victims:
                    self._buckets.pop(v_key, None)

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket
            # Drop timestamps that fell out of the window.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


def _client_key(request: Request) -> str:
    """Best-effort client identifier.

    Behind Cloud Run / load balancers the real IP arrives in
    ``X-Forwarded-For`` (the first entry is the client). Fall back to
    the direct socket address. We deliberately do NOT trust user-supplied
    headers like ``X-Real-IP`` if XFF is absent — that header is forged
    trivially.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip() or "unknown"
    client = request.client
    return (client.host if client else None) or "unknown"


def rate_limit(*, limit: int, window_seconds: float, name: str):
    """Build a FastAPI dependency that enforces a sliding-window limit."""
    window = _SlidingWindow(limit=limit, window_s=window_seconds)

    async def _dep(request: Request) -> None:
        key = f"{name}:{_client_key(request)}"
        if not window.hit(key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded for {name}: "
                    f"max {limit} requests per {int(window_seconds)}s. "
                    "Slow down and retry."
                ),
                headers={"Retry-After": str(int(window_seconds))},
            )

    return _dep


# Pre-built limiters for the costly fan-out endpoints. Tuned generously
# so a real user mashing the UI never trips them, but a script in a
# loop does within seconds.
resolve_limit = rate_limit(limit=60, window_seconds=60, name="cards.resolve")
search_live_limit = rate_limit(limit=60, window_seconds=60, name="cards.search")
scan_create_limit = rate_limit(limit=30, window_seconds=60, name="scans.create")


__all__ = [
    "rate_limit",
    "resolve_limit",
    "scan_create_limit",
    "search_live_limit",
]
