"""FastAPI application entrypoint for loupe-backend."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.config import get_settings
from app.exception_handlers import register_exception_handlers
from app.http_middleware import register_http_middleware
from app.lifecycle import lifespan
from app.observability import init_sentry
from app.response_envelope import register_envelope_middleware
from app.routers import (
    auth,
    cards,
    collections,
    grades,
    prices,
    providers,
    scanners,
    scans,
    sets,
    system,
    users,
    ws,
)


def create_app() -> FastAPI:
    """Factory used by uvicorn (``app.main:app``) and tests."""
    s = get_settings()

    # Wire Sentry first so any subsequent init failure is reported. The helper
    # is a graceful no-op when SENTRY_DSN is unset, so dev environments stay
    # zero-config.
    init_sentry(s)

    # Description text is loaded from documentation/render_openapi.py to keep this file lean.
    try:
        from documentation.render_openapi import build_full_description

        description = build_full_description()
    except Exception:  # pragma: no cover - documentation package optional in tests
        description = "Loupe backend API."

    app = FastAPI(
        title=s.app_name,
        version="0.1.0",
        description=description,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Middleware ordering note — Starlette executes the LAST-registered
    # middleware as the OUTERMOST layer.  Response flow we want is:
    #   router → envelope-wrap (innermost) → GZip → CORS → request-log (outer)
    # so the envelope sees the raw router JSON, GZip compresses the wrapped
    # body, and the request-log middleware stamps X-Request-Id on the final
    # outgoing response.
    register_envelope_middleware(app)  # innermost user middleware
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_http_middleware(
        app
    )  # outermost: must run first on request to set request-id
    register_exception_handlers(app)

    # System endpoints at root (no /v1 prefix).
    app.include_router(system.router)

    # Versioned API surface.
    api_prefix = "/v1"
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(users.router, prefix=api_prefix)
    app.include_router(scanners.router, prefix=api_prefix)
    app.include_router(scans.router, prefix=api_prefix)
    app.include_router(cards.router, prefix=api_prefix)
    app.include_router(sets.router, prefix=api_prefix)
    app.include_router(grades.router, prefix=api_prefix)
    app.include_router(prices.router, prefix=api_prefix)
    app.include_router(collections.router, prefix=api_prefix)
    app.include_router(providers.router, prefix=api_prefix)

    # WebSockets mount at root.
    app.include_router(ws.router)

    return app


app = create_app()

__all__ = ["app", "create_app"]
