"""Observability bootstrap — Sentry SDK + optional OpenTelemetry.

Both subsystems are *optional* and never crash the app when their SDKs
are missing or misconfigured — production stays up even if the
sidecar/exporter is wrong.

Sentry covers error capture + lightweight tracing (already enabled in
prod).

OpenTelemetry, when ``settings.otel_enabled`` is true, layers on
vendor-neutral distributed tracing across FastAPI handlers, SQLAlchemy
statements, outbound HTTPX calls, and Redis commands. Spans are exported
via the OTLP protocol; set the standard ``OTEL_EXPORTER_OTLP_ENDPOINT``
(and any auth header env vars) at deploy time — e.g. Cloud Trace's OTLP
HTTP endpoint with the gcloud auth proxy, or the dedicated GCP exporter
package.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

_sentry_initialized = False
_otel_initialized = False
_log = logging.getLogger("loupe.observability")


def init_sentry(settings: Settings) -> bool:
    """Initialize Sentry if a DSN is configured. Returns True on success."""
    global _sentry_initialized
    if _sentry_initialized:
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
        _sentry_initialized = True
        _log.info("sentry initialised env=%s", settings.app_env)
        return True
    except Exception:  # pragma: no cover - defensive; observability must not crash
        _log.exception("failed to initialise sentry; continuing without it")
        return False


def init_otel(settings: Settings, app: object | None = None) -> bool:
    """Bootstrap OpenTelemetry tracing if ``otel_enabled`` is true.

    Wires up the OTLP span exporter (read from the standard ``OTEL_*``
    env vars), a ratio sampler driven by ``settings.otel_sample_ratio``,
    and auto-instrumentations for FastAPI, SQLAlchemy, HTTPX, and Redis.

    Returns ``True`` on success. Missing SDK packages, missing exporter
    config, or any init failure is swallowed so the app keeps running.
    """
    global _otel_initialized
    if _otel_initialized or not settings.otel_enabled:
        return _otel_initialized
    try:  # pragma: no cover - optional dependency path exercised in prod
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

        provider = TracerProvider(
            resource=Resource.create(
                {
                    SERVICE_NAME: settings.otel_service_name,
                    "deployment.environment": settings.app_env,
                }
            ),
            sampler=TraceIdRatioBased(settings.otel_sample_ratio),
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)

        # Auto-instrument each subsystem if its package is installed; a
        # missing package on any one of them is non-fatal.
        for mod_path, cls_name, kwargs in (
            (
                "opentelemetry.instrumentation.fastapi",
                "FastAPIInstrumentor",
                {"app": app} if app is not None else {},
            ),
            (
                "opentelemetry.instrumentation.sqlalchemy",
                "SQLAlchemyInstrumentor",
                {},
            ),
            ("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor", {}),
            ("opentelemetry.instrumentation.redis", "RedisInstrumentor", {}),
        ):
            try:
                mod = __import__(mod_path, fromlist=[cls_name])
                instrumentor = getattr(mod, cls_name)()
                if kwargs:
                    instrumentor.instrument(**kwargs)
                else:
                    instrumentor.instrument()
            except Exception:
                _log.debug("otel: skipped %s", mod_path, exc_info=True)

        _otel_initialized = True
        _log.info(
            "opentelemetry initialised service=%s env=%s ratio=%.2f",
            settings.otel_service_name,
            settings.app_env,
            settings.otel_sample_ratio,
        )
        return True
    except Exception:  # pragma: no cover - defensive
        _log.exception("failed to initialise opentelemetry; continuing without it")
        return False


__all__ = ["init_otel", "init_sentry"]
