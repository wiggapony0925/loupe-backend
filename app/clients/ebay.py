"""eBay Finding/Browse API client stub.

Raises ``NotImplementedError`` when ``EBAY_APP_ID`` isn't configured.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings


def _require_app_id() -> str:
    s = get_settings()
    if not s.ebay_app_id:
        raise NotImplementedError("eBay App ID not configured (set EBAY_APP_ID).")
    return s.ebay_app_id


async def search_completed_listings(
    query: str, limit: int = 25
) -> list[dict[str, Any]]:
    """Return completed eBay listings matching ``query``."""
    _require_app_id()
    raise NotImplementedError(
        "eBay integration stub — wire up Browse/Finding API with %s." % query
    )


__all__ = ["search_completed_listings"]
