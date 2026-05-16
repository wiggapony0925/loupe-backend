"""Provider status endpoint — surface which real-data sources are live."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.integrations import get_registry

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("/status", summary="Real-data provider status (public)")
async def status() -> dict[str, Any]:
    """Return per-provider configuration + supported capabilities."""
    return {"providers": get_registry().status()}


__all__ = ["router"]
