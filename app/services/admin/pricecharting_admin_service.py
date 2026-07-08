"""Admin view over the PriceCharting integration — the data behind the
dev-portal "PriceCharting tier & fallback" page.

Surfaces the live-detected tier, the full fallback chain (with the active rung),
the grade-field mapping, and the local mirror status, plus actions to re-probe
capabilities and trigger a Legendary CSV sync.
"""

from __future__ import annotations

from typing import Any

from app.integrations.pricecharting import csv_sync, tiers


async def get_overview(*, force: bool = False) -> dict[str, Any]:
    """Everything the dev-portal page renders. ``force`` re-probes the live
    account instead of using the cached capability snapshot."""
    caps = await tiers.detect(force=force)
    mirror = await csv_sync.get_status()
    return tiers.describe(caps, mirror=mirror)


async def resync_mirror() -> dict[str, Any]:
    """Trigger a Legendary bulk CSV sync (no-op with a clear reason on lower
    tiers / when the CSV URL isn't configured)."""
    return await csv_sync.sync_price_guide()


__all__ = ["get_overview", "resync_mirror"]
