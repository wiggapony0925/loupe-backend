"""Remote app configuration — feature flags, force-update gate, layout hints.

Exposes a single ``GET /v1/app/config`` that the mobile client polls on
startup (and again on resume after >1h). The response lets us:

* toggle features without an App Store / Play Store release,
* reorder the Command-tab rails server-side,
* tell stale clients to force-update (``minSupportedVersion``).

This is the cheapest possible **server-driven UI** layer — full SDUI
(the Airbnb pattern of shipping component trees) can be layered on top
later without re-plumbing anything here.

Values live in code today so they ship with the backend deploy. Promoting
to a database table / Redis-backed dashboard is one ``CREATE TABLE`` away
when product needs it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services import ai
from app.services.admin import flag_service

router = APIRouter(prefix="/app", tags=["app"])

# Bumped whenever a *required* client capability lands. Clients below
# this version receive `forceUpdate: true` from /app/config and gate the
# UI on a blocking "Please update" sheet. Keep this conservative — only
# bump when an older client can no longer talk to the backend safely.
_MIN_SUPPORTED_VERSION = "0.1.0"

# Default Command-tab rail order. The client renders rails in the order
# returned here; unknown ids are skipped, so it's always safe to add new
# ones (clients that don't know the id ignore it).
_DEFAULT_HOME_RAILS: list[str] = [
    "portfolioChart",
    "topMovers",
    "hotRightNow",
    "recentScans",
]

# Discovery carousels below the personal feed on the mobile home tab.
# The BACKEND owns which rails show and in what order — reorder or drop
# an id here and every client follows on next config refresh, no release.
# Unknown ids are skipped client-side, so adding new ones is always safe.
_DEFAULT_DISCOVERY_RAILS: list[str] = [
    "trendingNow",
    "mostValuable",
    # Newest releases across the date-backed games — `/v1/sets?tcg=all&
    # sort=newest`. Sits with the product-discovery rails (sets → sealed).
    "newestSets",
    "sealedProducts",
    "stealsUnder5",
]

# Default feature flags. Add new flags here with a sensible default,
# then read them from the mobile client via `useAppConfig().flags.<name>`.
_DEFAULT_FLAGS: dict[str, bool] = {
    # PSA-10 cohort overlay on the Command-tab chart. Off by default
    # until cohort data quality is validated end-to-end.
    "showPsa10Overlay": True,
    # Server-driven Vault filtering. When false the client falls back
    # to downloading the full collection and filtering in-process.
    "serverDrivenVault": True,
    # Reanimated staggered entry on the Vault list. Cheap to ship as a
    # flag so we can disable it if low-end Androids complain.
    "vaultStaggerAnim": True,
}


@router.get(
    "/config",
    summary="Remote app configuration",
    description=(
        "Returns `{ minSupportedVersion, forceUpdate, flags, homeRails, "
        "discoveryRails, aiSearch }`. `forceUpdate` is server-computed from the "
        "optional `clientVersion` query param. `discoveryRails` orders the "
        "home-tab discovery carousels (unknown ids are skipped client-side). "
        "Clients should call this on cold start and again on resume after "
        ">1h, and persist the last response so launch is offline-tolerant."
    ),
)
async def get_app_config(
    clientVersion: str | None = None, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    # Dashboard-managed flags (the same `feature_flags` table the dev portal +
    # web read) override the baked-in defaults, so toggling a flag in the
    # dashboard reaches the app too — no release required.
    flags = dict(_DEFAULT_FLAGS)
    try:
        flags.update(await flag_service.public_map(db))
    except Exception:  # never let a flag read break the launch config
        pass
    return {
        "minSupportedVersion": _MIN_SUPPORTED_VERSION,
        "forceUpdate": _is_below_min(clientVersion, _MIN_SUPPORTED_VERSION),
        "flags": flags,
        "homeRails": list(_DEFAULT_HOME_RAILS),
        "discoveryRails": list(_DEFAULT_DISCOVERY_RAILS),
        # AI "describe it" search — limits AND the availability kill switch.
        # `enabled` flips false when no model key is set or recent failures
        # (quota, auth, outage) set a cooldown; clients HIDE the feature
        # entirely (sparkle button, slash command, callout) until it returns.
        # Served here so all of it reaches installed clients on the next
        # config refresh — no release.
        "aiSearch": {
            "enabled": await ai.available(),
            "queryMaxChars": ai.QUERY_MAX_CHARS,
            "messageMaxChars": ai.MESSAGE_MAX_CHARS,
        },
    }


def _is_below_min(client: str | None, minimum: str) -> bool:
    """True when ``client`` parses as a semver strictly older than ``minimum``.

    Tolerant of malformed input — anything we can't parse is treated as
    "current" so a buggy version string can never accidentally lock every
    user out of the app. If we ever need stricter behaviour we can flip
    that default and ship a coordinated client release first.
    """
    if not client:
        return False
    try:
        c = _parse_semver(client)
        m = _parse_semver(minimum)
    except ValueError:
        return False
    return c < m


def _parse_semver(s: str) -> tuple[int, int, int]:
    parts = s.strip().split("-")[0].split("+")[0].split(".")
    if len(parts) < 1 or len(parts) > 3:
        raise ValueError(f"invalid version: {s!r}")
    nums = [int(p) for p in parts]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


__all__ = ["router"]
