"""Sports Card Investor (SCI) client stub."""

from __future__ import annotations

from typing import Any

from app.config import get_settings


def _require_token() -> str:
    s = get_settings()
    if not s.sci_api_key:
        raise NotImplementedError("SCI key not configured (set SCI_API_KEY).")
    return s.sci_api_key


async def lookup_card(query: str) -> dict[str, Any] | None:
    """Look up a sports card by name."""
    _require_token()
    raise NotImplementedError(f"SCI integration stub — query={query!r}.")


__all__ = ["lookup_card"]
