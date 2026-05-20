"""Provider status endpoint — surface which real-data sources are live."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import require_user
from app.integrations import get_registry
from app.integrations.psa import PsaProvider
from app.models.user import User

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("/status", summary="Real-data provider status (public)")
async def get_status() -> dict[str, Any]:
    """Return per-provider configuration + supported capabilities."""
    return {"providers": get_registry().status()}


@router.get(
    "/psa/cert/{cert_no}",
    summary="Verify a PSA cert number (authed; cached 24h)",
)
async def verify_psa_cert(
    cert_no: str, _user: User = Depends(require_user)
) -> dict[str, Any]:
    """Look up a PSA cert via the PSA Public API.

    Free PSA tier is capped at 100 requests/day across the whole app, so
    results are cached in Redis for 24h. Auth required so anonymous traffic
    can't burn the quota.
    """
    provider = next(
        (p for p in get_registry().all if isinstance(p, PsaProvider)), None
    )
    if provider is None or not provider.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PSA provider not configured",
        )
    cert = await provider.verify_cert(cert_no)
    if cert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PSA cert not found",
        )
    return {"cert": cert}


__all__ = ["router"]
