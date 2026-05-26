"""Middleware that auto-wraps unwrapped router JSON responses into the envelope.

Routers can either return the envelope themselves (via
:func:`app.schemas.envelope.ok` / :func:`~app.schemas.envelope.page`) or return
raw payloads — this middleware notices unwrapped payloads, lifts legacy
``{"items", "total", "page", "page_size"}`` dicts into the pagination block,
and stamps the envelope ``meta`` onto every successful response.

System endpoints (``/health``, ``/version``, ``/metrics``) and the OpenAPI
docs are exempt by path prefix.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from starlette.middleware.base import BaseHTTPMiddleware

from app.schemas.envelope import build_meta, build_pagination

# Paths whose responses MUST NOT be wrapped (load-balancer simplicity, OpenAPI).
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/health",
    "/version",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/ws",  # WebSockets are their own protocol
)


def _is_exempt(path: str) -> bool:
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


def _looks_like_envelope(body: object) -> bool:
    """Return ``True`` if *body* is already in envelope shape."""
    return (
        isinstance(body, dict)
        and "meta" in body
        and ("data" in body or "error" in body)
    )


def _looks_like_legacy_pagination(body: object) -> bool:
    """Detect the legacy ``Pagination[...]`` shape so we can lift it."""
    return (
        isinstance(body, dict)
        and {"items", "total", "page", "page_size"}.issubset(body.keys())
        and isinstance(body.get("items"), list)
        and isinstance(body.get("total"), int)
    )


class EnvelopeMiddleware(BaseHTTPMiddleware):
    """Wrap successful JSON responses in the universal envelope."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)

        if _is_exempt(request.url.path):
            return response
        # Streaming / non-JSON responses pass through.
        media_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        if media_type != "application/json":
            return response
        # 204 has no body to wrap.
        if response.status_code == 204:
            return response

        # Collect the raw bytes — ``call_next`` returns a streaming response.
        body_bytes = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body_bytes += chunk
        try:
            payload = json.loads(body_bytes) if body_bytes else None
        except json.JSONDecodeError:
            # Not JSON-parseable — pass through untouched.
            return Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=media_type,
            )

        # Already enveloped (e.g. exception handlers, helpers).
        if not isinstance(payload, dict):
            # Bare JSON literal (list / number / string / null) — wrap as data.
            envelope_dict = {
                "data": payload,
                "meta": jsonable_encoder(build_meta()),
                "pagination": None,
                "error": None,
            }
        elif _looks_like_envelope(payload):
            envelope_dict = payload
        elif _looks_like_legacy_pagination(payload):
            pagination = build_pagination(
                total=int(payload["total"]),
                page=int(payload["page"]),
                page_size=int(payload["page_size"]),
            )
            envelope_dict = {
                "data": payload["items"],
                "meta": jsonable_encoder(build_meta()),
                "pagination": jsonable_encoder(pagination),
                "error": None,
            }
        else:
            envelope_dict = {
                "data": payload,
                "meta": jsonable_encoder(build_meta()),
                "pagination": None,
                "error": None,
            }

        new_body = json.dumps(envelope_dict).encode("utf-8")
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=new_body,
            status_code=response.status_code,
            headers=headers,
            media_type="application/json",
        )


def register_envelope_middleware(app: FastAPI) -> None:
    """Attach the envelope-wrapping middleware to *app*."""
    app.add_middleware(EnvelopeMiddleware)


__all__ = [
    "EnvelopeMiddleware",
    "register_envelope_middleware",
]
