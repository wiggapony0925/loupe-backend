"""Observability bootstrap — optional Sentry SDK initialization.

The application stays fully functional when ``sentry_dsn`` is unset; the
SDK is imported lazily and any failure is swallowed so production never
crashes because the observability sidecar is misconfigured.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

_initialized = False
_log = logging.getLogger("loupe.observability")


def init_sentry(settings: "Settings") -> bool:
    """Initialize Sentry if a DSN is configured. Returns True on success."""
    global _initialized
    if _initialized:
        return True
    dsn = settings.sentry_dsn
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=settings.app_env,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            profiles_sample_rate=settings.sentry_profiles_sample_rate,
            send_default_pii=False,
            integrations=[
                StarletteIntegration(),
                FastApiIntegration(),
                AsyncioIntegration(),
            ],
        )
        _initialized = True
        _log.info("sentry initialised env=%s", settings.app_env)
        return True
    except Exception:  # pragma: no cover - defensive; observability must not crash
        _log.exception("failed to initialise sentry; continuing without it")
        return False


__all__ = ["init_sentry"]
