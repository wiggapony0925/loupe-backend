"""Global exception → envelope mappers registered on the FastAPI app.

These ensure every non-2xx response shares the envelope shape defined in
:mod:`app.schemas.envelope` so the frontend can rely on a single contract.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.envelope import fail
from app.utils.logger import get_logger

logger = get_logger("exception_handlers")


# Map HTTP status → default error.code when the router didn't specify one.
_STATUS_CODE_DEFAULTS: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "request.bad",
    status.HTTP_401_UNAUTHORIZED: "auth.invalid_token",
    status.HTTP_403_FORBIDDEN: "auth.forbidden",
    status.HTTP_404_NOT_FOUND: "resource.not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "request.method_not_allowed",
    status.HTTP_409_CONFLICT: "resource.conflict",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "validation.failed",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limit.exceeded",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "internal.unexpected",
    status.HTTP_502_BAD_GATEWAY: "upstream.unavailable",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service.unavailable",
    status.HTTP_504_GATEWAY_TIMEOUT: "upstream.timeout",
}


def _default_code(http_status: int) -> str:
    return _STATUS_CODE_DEFAULTS.get(http_status, f"error.{http_status}")


def _envelope_response(env_dict: dict[str, Any], status_code: int) -> JSONResponse:
    return JSONResponse(content=env_dict, status_code=status_code)


async def http_exception_handler(
    _request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Translate ``HTTPException`` → envelope error response."""
    detail = exc.detail
    code: str
    message: str
    field: str | None = None
    details: Any | None = None
    if isinstance(detail, dict):
        code = str(detail.get("code") or _default_code(exc.status_code))
        message = str(detail.get("message") or detail.get("detail") or exc.status_code)
        field = detail.get("field") if isinstance(detail.get("field"), str) else None
        details = detail.get("details")
    else:
        code = _default_code(exc.status_code)
        message = str(detail) if detail is not None else "HTTP error"

    env = fail(
        code=code, message=message, status=exc.status_code, field=field, details=details
    )
    return _envelope_response(jsonable_encoder(env), exc.status_code)


async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Translate Pydantic ``RequestValidationError`` → envelope 422."""
    errors = exc.errors()
    field = None
    if errors:
        loc = errors[0].get("loc") or ()
        if loc:
            field = ".".join(str(p) for p in loc if p not in ("body", "query", "path"))
    env = fail(
        code="validation.failed",
        message="Request validation failed.",
        status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        field=field,
        details=jsonable_encoder(errors),
    )
    return _envelope_response(
        jsonable_encoder(env), status.HTTP_422_UNPROCESSABLE_ENTITY
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unexpected exceptions — returns envelope 500."""
    logger.exception(
        "Unhandled %s on %s: %s", type(exc).__name__, request.url.path, exc
    )
    env = fail(
        code="internal.unexpected",
        message="An unexpected error occurred.",
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
    return _envelope_response(
        jsonable_encoder(env), status.HTTP_500_INTERNAL_SERVER_ERROR
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire all envelope-aware exception handlers onto *app*.

    Starlette's ``add_exception_handler`` types its callback as accepting
    the broad ``Exception`` base class; our handlers are intentionally
    narrowed to their concrete exception type so we cast to ``Any`` to keep
    mypy quiet without weakening the actual handler signatures.
    """
    app.add_exception_handler(
        StarletteHTTPException, cast("Any", http_exception_handler)
    )
    app.add_exception_handler(HTTPException, cast("Any", http_exception_handler))
    app.add_exception_handler(
        RequestValidationError, cast("Any", validation_exception_handler)
    )
    app.add_exception_handler(Exception, cast("Any", unhandled_exception_handler))


__all__ = [
    "http_exception_handler",
    "register_exception_handlers",
    "unhandled_exception_handler",
    "validation_exception_handler",
]
