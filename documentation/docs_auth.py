"""Scalar-powered API documentation routes.

Mounts a single ``/api-docs`` page (with ``/docs`` and ``/redoc`` redirects)
that serves a static HTML wrapper around Scalar's CDN bundle, pointing at the
existing ``/openapi.json`` schema.

Optional access-gate: set ``DOCS_ACCESS_TOKEN`` in the environment to require
``?token=<value>`` on first visit; a signed session cookie keeps the user
authenticated for subsequent loads. When unset (default), docs are public.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

_DOCS_TOKEN = os.environ.get("DOCS_ACCESS_TOKEN", "").strip()
_COOKIE_NAME = "loupe_docs_session"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def register_docs_routes(app: FastAPI, *, static_dir: Path) -> None:
    """Mount ``/api-docs`` (Scalar) and redirect the FastAPI defaults to it."""
    docs_html_path = static_dir / "docs" / "index.html"
    original_openapi = app.openapi

    @app.get("/api-docs", include_in_schema=False)
    async def scalar_docs(request: Request) -> HTMLResponse:
        if not _verify_docs_access(request):
            return _denied_response()

        response = HTMLResponse(docs_html_path.read_text())
        query_token = request.query_params.get("token", "")
        if (
            _DOCS_TOKEN
            and query_token
            and secrets.compare_digest(query_token, _DOCS_TOKEN)
        ):
            _set_session_cookie(response, _DOCS_TOKEN, request)
        return response

    @app.get("/docs", include_in_schema=False)
    async def docs_redirect(request: Request) -> RedirectResponse:
        token = request.query_params.get("token", "")
        url = "/api-docs" + (f"?token={token}" if token else "")
        return RedirectResponse(url=url)

    @app.get("/redoc", include_in_schema=False)
    async def redoc_redirect(request: Request) -> RedirectResponse:
        token = request.query_params.get("token", "")
        url = "/api-docs" + (f"?token={token}" if token else "")
        return RedirectResponse(url=url)

    @app.get("/openapi.json", include_in_schema=False)
    async def protected_openapi(request: Request) -> JSONResponse:
        if not _verify_docs_access(request):
            return JSONResponse({"detail": "Access token required"}, status_code=403)
        return JSONResponse(original_openapi())


def _sign_cookie(token: str) -> str:
    return hmac.new(token.encode(), b"loupe-docs-session", hashlib.sha256).hexdigest()


def _verify_docs_access(request: Request) -> bool:
    if not _DOCS_TOKEN:
        return True
    query_token = request.query_params.get("token", "")
    if query_token and secrets.compare_digest(query_token, _DOCS_TOKEN):
        return True
    cookie_value = request.cookies.get(_COOKIE_NAME, "")
    expected = _sign_cookie(_DOCS_TOKEN)
    return bool(cookie_value and secrets.compare_digest(cookie_value, expected))


def _set_session_cookie(response: HTMLResponse, token: str, request: Request) -> None:
    response.set_cookie(
        key=_COOKIE_NAME,
        value=_sign_cookie(token),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def _denied_response() -> HTMLResponse:
    return HTMLResponse(
        content=(
            "<!DOCTYPE html><html><body style='font-family:system-ui;"
            "background:#0B0B0D;color:#F5F5F7;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'>"
            "<h1 style='color:#00F59B'>Loupe API Docs</h1>"
            "<p>Access token required.</p>"
            "<p style='color:#A1A1A6;font-size:14px'>Append <code>?token=&lt;value&gt;</code> to the URL.</p>"
            "</div></body></html>"
        ),
        status_code=403,
    )
