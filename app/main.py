"""FastAPI application entrypoint for loupe-backend."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.config import get_settings
from app.http_middleware import register_http_middleware
from app.lifecycle import lifespan
from app.routers import (
    auth,
    cards,
    collections,
    grades,
    prices,
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

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_http_middleware(app)

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

    # WebSockets mount at root.
    app.include_router(ws.router)

    return app


app = create_app()

__all__ = ["app", "create_app"]
