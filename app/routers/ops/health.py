"""System endpoints: ``/health``, ``/version``, lightweight ``/metrics``."""

from __future__ import annotations

import time

from fastapi import APIRouter

from app import __version__
from app.platform.redis_client import get_redis
from app.config import get_settings

router = APIRouter(tags=["system"])

_BOOT_TIME = time.time()


@router.get("/health", summary="Liveness / readiness probe")
async def health() -> dict[str, object]:
    """Return basic process + dependency health information."""
    redis = await get_redis()
    redis_ok = False
    try:
        redis_ok = bool(await redis.ping())
    except Exception:
        redis_ok = False
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _BOOT_TIME, 3),
        "redis": "ok" if redis_ok else "stub",
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
