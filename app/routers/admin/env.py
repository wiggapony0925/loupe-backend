"""Admin environment manager (`/v1/admin/env`).

Read-only catalog of the backend's environment configuration — presence, group,
docs link, and (for non-secret config only) the value. Secrets are never echoed.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas.ops import EnvReport
from app.services.admin import env_service

router = APIRouter(prefix="/env", tags=["admin-ops"])


@router.get("", response_model=EnvReport, summary="Environment configuration")
async def get_env() -> EnvReport:
    return env_service.report()


__all__ = ["router"]
