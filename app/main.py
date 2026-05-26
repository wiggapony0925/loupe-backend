"""FastAPI application entrypoint for loupe-backend."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.config import get_settings
from app.exception_handlers import register_exception_handlers
from app.http_middleware import register_http_middleware
from app.lifecycle import lifespan
from app.observability import init_sentry
from app.response_envelope import register_envelope_middleware
from app.routers.analytics import home_feed as home
from app.routers.analytics import portfolio_overview as analytics
from app.routers.analytics import reports
from app.routers.auth import auth, users
from app.routers.catalog import card_sets as sets
from app.routers.catalog import cards, identify, providers
from app.routers.collection import collections, grades, scans, sealed
from app.routers.collection import scan_devices as scanners
from app.routers.market import alerts, market, prices
from app.routers.ops import app_config
from app.routers.ops import health as system
from app.routers.ops import websockets as ws


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
        docs_url=None,  # disable Swagger UI — we serve Scalar at /api-docs
        redoc_url=None,  # disable Redoc — redirected to /api-docs
        openapi_url=None,  # disable built-in /openapi.json — re-served by docs_auth
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

    # Scalar-powered API documentation at /api-docs (replaces Swagger/Redoc).
    # Also serves the openapi.json behind the same (optional) access gate.
    try:
        from documentation.docs_auth import register_docs_routes

        register_docs_routes(app, static_dir=Path(__file__).parent / "static")
    except Exception:  # pragma: no cover - docs are non-critical at boot
        pass

    # System endpoints at root (no /v1 prefix).
    app.include_router(system.router)

    # Versioned API surface.
    api_prefix = "/v1"
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(users.router, prefix=api_prefix)
    app.include_router(scanners.router, prefix=api_prefix)
    app.include_router(scans.router, prefix=api_prefix)
    app.include_router(cards.router, prefix=api_prefix)
    app.include_router(identify.router, prefix=api_prefix)
    app.include_router(sets.router, prefix=api_prefix)
    app.include_router(grades.router, prefix=api_prefix)
    app.include_router(prices.router, prefix=api_prefix)
    app.include_router(alerts.router, prefix=api_prefix)
    app.include_router(collections.router, prefix=api_prefix)
    app.include_router(market.router, prefix=api_prefix)
    app.include_router(providers.router, prefix=api_prefix)
    app.include_router(reports.router, prefix=api_prefix)
    app.include_router(app_config.router, prefix=api_prefix)
    app.include_router(home.router, prefix=api_prefix)
    app.include_router(analytics.router, prefix=api_prefix)
    app.include_router(sealed.catalog_router, prefix=api_prefix)
    app.include_router(sealed.holdings_router, prefix=api_prefix)

    # WebSockets mount at root.
    app.include_router(ws.router)

    return app


app = create_app()

__all__ = ["app", "create_app"]
