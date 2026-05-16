"""HTTP middleware: request logging + Cache-Control header injection.

The middleware also populates the request-scoped :mod:`app.request_context`
ContextVars so the envelope/meta builder downstream can stamp every response
with the same ``request_id`` and compute ``duration_ms``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.request_context import (
    new_request_id,
    set_request_id,
    set_request_started_at,
    set_request_user_id,
)
from app.utils.logger import get_logger

_log = get_logger("http")

# Path-prefix → Cache-Control header. First match wins.
_CACHE_CONTROL_RULES: list[tuple[str, str]] = [
    ("/health", "no-store"),
    ("/version", "public, max-age=60"),
    ("/sets", "public, max-age=3600, stale-while-revalidate=86400"),
    ("/cards", "public, max-age=300, stale-while-revalidate=3600"),
    ("/me", "private, no-store"),
    ("/scans", "private, no-store"),
    ("/grades", "private, max-age=15"),
    ("/collections", "private, no-store"),
]

# Paths whose successful (2xx/3xx) requests are demoted to DEBUG so the
# access log isn't flooded by Cloud Run health probes and OPTIONS noise.
# Failures still surface at WARN/ERROR.
_QUIET_PATH_PREFIXES: tuple[str, ...] = ("/health", "/version", "/metrics")


def resolve_cache_control(path: str) -> str | None:
    """Return the appropriate ``Cache-Control`` header for *path*, if any."""
    for prefix, value in _CACHE_CONTROL_RULES:
        if path.startswith(prefix):
            return value
    return None


def _is_quiet_path(path: str) -> bool:
    return any(path.startswith(p) for p in _QUIET_PATH_PREFIXES)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Log every request and stamp ``X-Request-Id`` on every response."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        req_id = request.headers.get("x-request-id") or new_request_id()
        start = time.perf_counter()
        set_request_id(req_id)
        set_request_started_at(start)
        set_request_user_id(None)  # auth dep will populate later
        method = request.method
        path = request.url.path
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            _log.exception(
                "%s %s -> EXC (%.1fms)",
                method,
                path,
                elapsed_ms,
                extra={
                    "event": "http.request",
                    "http_method": method,
                    "http_path": path,
                    "http_status": 500,
                    "latency_ms": round(elapsed_ms, 1),
                    "outcome": "exception",
                },
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        cache_value = resolve_cache_control(path)
        if cache_value and "cache-control" not in (k.lower() for k in response.headers):
            response.headers["Cache-Control"] = cache_value
        response.headers["X-Request-Id"] = req_id

        status_code = response.status_code
        # Choose level: errors → WARNING/ERROR; quiet successes → DEBUG.
        if status_code >= 500:
            level = "error"
        elif status_code >= 400:
            level = "warning"
        elif _is_quiet_path(path):
            level = "debug"
        else:
            level = "info"

        log_fn = getattr(_log, level)
        log_fn(
            "%s %s -> %d (%.1fms)",
            method,
            path,
            status_code,
            elapsed_ms,
            extra={
                "event": "http.request",
                "http_method": method,
                "http_path": path,
                "http_status": status_code,
                "latency_ms": round(elapsed_ms, 1),
                "outcome": "ok" if status_code < 400 else "error",
            },
        )
        return response


def register_http_middleware(app: FastAPI) -> None:
    """Attach the request-log middleware to *app*."""
    app.add_middleware(RequestLogMiddleware)


__all__ = [
    "RequestLogMiddleware",
    "register_http_middleware",
    "resolve_cache_control",
]
