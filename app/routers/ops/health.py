"""System endpoints: ``/health``, ``/version``, lightweight ``/metrics``."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import get_settings
from app.db import get_db
from app.platform.redis_client import get_redis

router = APIRouter(tags=["system"])

_BOOT_TIME = time.time()


@router.get("/health", summary="Liveness / readiness probe")
async def health(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Return basic process + dependency health information.

    ``status`` stays "ok" as long as the PROCESS is healthy — dependency
    states are reported per-key so dashboards/alerts can distinguish "app
    is down" from "app is up but a dependency is degraded" without the
    probe itself recycling instances during a broker blip.
    """
    redis = await get_redis()
    redis_ok = False
    try:
        redis_ok = bool(await redis.ping())
    except Exception:
        redis_ok = False
    db_ok = False
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _BOOT_TIME, 3),
        "redis": "ok" if redis_ok else "stub",
        "db": "ok" if db_ok else "down",
    }


@router.get("/version", summary="Build / version metadata")
async def version() -> dict[str, str]:
    s = get_settings()
    return {
        "name": s.app_name,
        "version": __version__,
        "env": s.app_env,
    }


@router.get("/metrics", summary="Minimal text metrics")
async def metrics() -> dict[str, float]:
    return {
        "uptime_seconds": round(time.time() - _BOOT_TIME, 3),
    }


__all__ = ["router"]
