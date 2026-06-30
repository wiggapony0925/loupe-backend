"""Per-integration monthly request budget, metered in Redis.

Some upstreams we depend on are billed per request with a hard free-tier
ceiling — apitcg's free plan is **1000 requests / month**. With many users that
ceiling is trivial to blow through if every page view fans out to the upstream,
so the catalog layer serves reads from a long-lived Redis cache (see
``cache_swr``) and only syncs from the upstream a handful of times a month.

This meter is the safety net under that strategy: it counts every upstream call
in a per-month Redis counter and lets callers ask "do I have budget to spend?"
before making one. When the soft ceiling is reached the catalog layer keeps
serving cached/stale data instead of calling the upstream, so we never exceed
the free tier no matter how much traffic arrives.

Design notes
------------
* **Atomic.** Uses Redis ``INCRBY`` so concurrent workers share one true count.
* **Self-expiring.** The counter key is namespaced by ``YYYY-MM`` and expires a
  few days into the next month, so the budget naturally resets — no cron.
* **Fails open.** If Redis is unreachable the meter never *blocks* traffic
  (``can_spend`` returns ``True``); the long cache TTLs are the real volume
  control, this is the visible ceiling + guard rail on top.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.platform.redis_client import get_redis
from app.utils.logger import get_logger

logger = get_logger("platform.api_budget")

#: Counter lives ~5 days into the next month before expiring, leaving a window
#: to read "last month's" usage for admin/observability before it rolls off.
_KEY_TTL_SECONDS = 35 * 24 * 60 * 60


def _current_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


class ApiBudget:
    """Monthly request budget for one upstream integration.

    Parameters
    ----------
    integration:
        Stable slug used in the Redis key (e.g. ``"apitcg"``).
    monthly_limit:
        The provider's hard request ceiling for the billing month.
    soft_ratio:
        Fraction of ``monthly_limit`` at which :meth:`can_spend` starts saying
        no, leaving headroom for in-flight/uncounted calls. ``0.9`` → stop at
        90% used.
    """

    def __init__(
        self, integration: str, monthly_limit: int, *, soft_ratio: float = 0.9
    ) -> None:
        self.integration = integration
        self.monthly_limit = max(0, int(monthly_limit))
        self.soft_ratio = soft_ratio

    def _key(self, period: str | None = None) -> str:
        return f"loupe:budget:{self.integration}:{period or _current_period()}"

    @property
    def soft_ceiling(self) -> int:
        return int(self.monthly_limit * self.soft_ratio)

    async def used(self) -> int:
        """Requests spent in the current month (0 if unknown/Redis down)."""
        try:
            r = await get_redis()
            raw = await r.get(self._key())
            return int(raw) if raw is not None else 0
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("api_budget used() failed for %s: %s", self.integration, exc)
            return 0

    async def remaining(self) -> int:
        return max(0, self.monthly_limit - await self.used())

    async def can_spend(self, n: int = 1) -> bool:
        """True if spending ``n`` more requests stays under the soft ceiling.

        Fails open: any Redis error returns ``True`` so the meter never takes
        the site down — the cache TTLs remain the real protection.
        """
        if self.monthly_limit <= 0:
            return True
        try:
            return (await self.used()) + n <= self.soft_ceiling
        except Exception:  # pragma: no cover
            return True

    async def spend(self, n: int = 1) -> int:
        """Record ``n`` spent requests; returns the new monthly total."""
        try:
            r = await get_redis()
            key = self._key()
            total = await r.incrby(key, n)
            # First write of the month → arm the rolling expiry.
            if total <= n:
                await r.expire(key, _KEY_TTL_SECONDS)
            return int(total)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("api_budget spend() failed for %s: %s", self.integration, exc)
            return 0

    async def usage(self) -> dict[str, object]:
        """Snapshot for admin/observability surfaces."""
        used = await self.used()
        return {
            "integration": self.integration,
            "period": _current_period(),
            "used": used,
            "limit": self.monthly_limit,
            "remaining": max(0, self.monthly_limit - used),
            "soft_ceiling": self.soft_ceiling,
            "exhausted": used >= self.soft_ceiling,
        }


__all__ = ["ApiBudget"]
