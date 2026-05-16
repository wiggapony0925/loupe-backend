"""TCGplayer pricing client.

TCGplayer requires per-partner OAuth credentials.  When credentials are not
configured the helpers in this module raise a clear ``NotImplementedError``
so callers can degrade gracefully and operators know what's missing.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings


def _require_credentials() -> tuple[str, str]:
    s = get_settings()
    if not (s.tcgplayer_client_id and s.tcgplayer_client_secret):
        raise NotImplementedError(
            "TCGplayer credentials are not configured "
            "(set TCGPLAYER_CLIENT_ID and TCGPLAYER_CLIENT_SECRET)."
        )
    return s.tcgplayer_client_id, s.tcgplayer_client_secret


async def get_market_prices(product_id: int) -> dict[str, Any]:
    """Return the latest market prices for a TCGplayer product."""
    _require_credentials()
    raise NotImplementedError(
        "TCGplayer integration stub — wire up OAuth + /pricing/product/{id}."
    )


__all__ = ["get_market_prices"]
