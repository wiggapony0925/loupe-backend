"""FastAPI application entrypoint for loupe-backend."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.config import get_settings
from app.http.exception_handlers import register_exception_handlers
from app.http.middleware import register_http_middleware
from app.http.response_envelope import register_envelope_middleware
from app.lifecycle import lifespan
from app.platform.observability import init_otel, init_sentry
from app.platform.startup_checks import validate_production_config
from app.routers import billing, email_webhook
from app.routers import notifications as me_notifications
from app.routers.admin import admin_router
from app.routers.analytics import home_feed as home
from app.routers.analytics import portfolio_overview as analytics
from app.routers.analytics import reports
from app.routers.auth import auth, users
from app.routers.catalog import card_sets as sets
from app.routers.catalog import cards, identify, image_proxy, providers
from app.routers.collection import collections, grades, scans, sealed, watchlist
from app.routers.collection import scan_devices as scanners
from app.routers.market import alerts, market, prices
from app.routers.ops import announcement, app_config
from app.routers.ops import feature_flags as flags
from app.routers.ops import health as system
from app.routers.ops import websockets as ws
from app.routers.portal import blog as portal_blog
from app.routers.portal import careers as portal_careers
from app.routers.portal import waitlist as portal_waitlist
from app.routers.public import share as public_share
from app.routers.public import storefront as public
from app.routers.public import stores as public_stores
from app.routers.public import unsubscribe as public_unsubscribe
from app.routers.public import verify_email as public_verify_email
from app.social.router import router as social_router


def create_app() -> FastAPI:
    """Factory used by uvicorn (``app.main:app``) and tests."""
    s = get_settings()

    # Refuse to boot on an insecure production configuration (no-op in dev/test).
    validate_production_config(s)

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
    # The signed-in user's inbox (/v1/me/notifications).
    app.include_router(me_notifications.router, prefix=api_prefix)
    app.include_router(scanners.router, prefix=api_prefix)
    app.include_router(scans.router, prefix=api_prefix)
    app.include_router(cards.router, prefix=api_prefix)
    app.include_router(public.router, prefix=api_prefix)
    app.include_router(public_stores.router, prefix=api_prefix)
    app.include_router(public_share.router, prefix=api_prefix)
    app.include_router(public_unsubscribe.router, prefix=api_prefix)
    app.include_router(public_verify_email.router, prefix=api_prefix)
    app.include_router(identify.router, prefix=api_prefix)
    app.include_router(image_proxy.router, prefix=api_prefix)
    app.include_router(sets.router, prefix=api_prefix)
    app.include_router(grades.router, prefix=api_prefix)
    app.include_router(prices.router, prefix=api_prefix)
    app.include_router(alerts.router, prefix=api_prefix)
    app.include_router(collections.router, prefix=api_prefix)
    app.include_router(watchlist.router, prefix=api_prefix)
    app.include_router(market.router, prefix=api_prefix)
    app.include_router(providers.router, prefix=api_prefix)
    app.include_router(reports.router, prefix=api_prefix)
    app.include_router(app_config.router, prefix=api_prefix)
    app.include_router(home.router, prefix=api_prefix)
    app.include_router(analytics.router, prefix=api_prefix)
    app.include_router(sealed.catalog_router, prefix=api_prefix)
    app.include_router(sealed.holdings_router, prefix=api_prefix)
    # Developer portal — public careers/blog/waitlist + admin management.
    app.include_router(portal_careers.router, prefix=api_prefix)
    app.include_router(portal_blog.router, prefix=api_prefix)
    app.include_router(portal_waitlist.router, prefix=api_prefix)
    app.include_router(flags.router, prefix=api_prefix)
    # Community layer (follows, profiles, shared collections) — app/social.
    app.include_router(social_router, prefix=api_prefix)
    app.include_router(announcement.router, prefix=api_prefix)
    app.include_router(billing.router, prefix=api_prefix)
    app.include_router(email_webhook.router, prefix=api_prefix)
    app.include_router(admin_router, prefix=api_prefix)

    # WebSockets mount at root.
    app.include_router(ws.router)

    # Wire OpenTelemetry last so FastAPI instrumentation sees every route.
    # No-op unless ``otel_enabled`` is true; safe for tests and dev.
    init_otel(s, app=app)

    return app


app = create_app()

__all__ = ["app", "create_app"]
