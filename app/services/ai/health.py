"""Loupe AI health — the automatic kill switch.

When the model misbehaves — quota exhausted, key revoked, provider outage —
the FEATURE disappears from every client instead of showing broken states:
failures set a kv_cache cooldown here, ``/v1/app/config`` publishes
``aiSearch.enabled`` from :func:`available`, and the clients hide the sparkle
button / slash command / callout entirely until the cooldown lapses. kv_cache
is shared Postgres, so one instance's failure silences the feature fleet-wide.

Billing-class errors (quota / auth) cool down for hours — retrying can't fix
a spent key. Transient errors (timeouts, 5xx) cool down for minutes.
"""

from __future__ import annotations

from app.platform.cache_l2 import kv_delete, kv_get, kv_set
from app.services.ai.providers import configured
from app.utils.logger import get_logger

logger = get_logger("services.ai.health")

_DISABLED_KEY = "ai_search:disabled:v1"

#: Quota / auth / billing failures — a retry can't fix these; back off long.
QUOTA_COOLDOWN_SECONDS = 6 * 60 * 60
#: Everything else (timeouts, provider 5xx) — short pause, then try again.
TRANSIENT_COOLDOWN_SECONDS = 10 * 60

#: Markers of billing-class failures across provider SDK exception messages.
_QUOTA_MARKERS = (
    "insufficient_quota",
    "quota",
    "billing",
    "rate_limit",
    "429",
    "invalid_api_key",
    "authentication",
    "401",
)


async def available() -> bool:
    """Whether the AI feature should exist right now (key set + no cooldown)."""
    if not configured():
        return False
    return await kv_get(_DISABLED_KEY) is None


async def record_failure(exc: Exception) -> None:
    """A model call failed — cool the feature down fleet-wide.

    Best effort: health bookkeeping must never break the request (the caller
    is already falling back to plain search).
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    quota = any(marker in text for marker in _QUOTA_MARKERS)
    ttl = QUOTA_COOLDOWN_SECONDS if quota else TRANSIENT_COOLDOWN_SECONDS
    logger.warning(
        "ai search cooling down for %ss (%s failure): %s",
        ttl,
        "quota/auth" if quota else "transient",
        exc,
    )
    await kv_set(_DISABLED_KEY, "quota" if quota else "transient", ttl)


async def reset() -> None:
    """Clear a cooldown (ops/testing escape hatch)."""
    await kv_delete(_DISABLED_KEY)


__all__ = [
    "QUOTA_COOLDOWN_SECONDS",
    "TRANSIENT_COOLDOWN_SECONDS",
    "available",
    "record_failure",
    "reset",
]
