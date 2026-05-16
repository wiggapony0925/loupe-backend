"""PriceCharting client stub."""

from __future__ import annotations

from typing import Any

from app.config import get_settings


def _require_token() -> str:
    s = get_settings()
    if not s.pricecharting_api_key:
        raise NotImplementedError(
            "PriceCharting key not configured (set PRICECHARTING_API_KEY)."
        )
    return s.pricecharting_api_key


async def lookup_product(query: str) -> dict[str, Any] | None:
    """Look up a PriceCharting product by name."""
    _require_token()
    raise NotImplementedError(f"PriceCharting integration stub — query={query!r}.")


__all__ = ["lookup_product"]
