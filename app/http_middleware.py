"""HTTP middleware: request logging + Cache-Control header injection."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

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


def resolve_cache_control(path: str) -> str | None:
    """Return the appropriate ``Cache-Control`` header for *path*, if any."""
    for prefix, value in _CACHE_CONTROL_RULES:
        if path.startswith(prefix):
            return value
    return None


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Log every request with method, path, status, latency and request-id."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        req_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            _log.exception(
                "%s %s -> EXC (%.1fms) [req=%s]",
                request.method,
                request.url.path,
                elapsed_ms,
                req_id,
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        cache_value = resolve_cache_control(request.url.path)
        if cache_value and "cache-control" not in (k.lower() for k in response.headers):
            response.headers["Cache-Control"] = cache_value
        response.headers["X-Request-Id"] = req_id

        _log.info(
            "%s %s -> %d (%.1fms) [req=%s]",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            req_id,
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
