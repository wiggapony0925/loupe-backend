"""FastAPI dependencies for authenticated request handling."""

from __future__ import annotations

import uuid

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import verify_token
from app.config import get_settings
from app.db import get_db
from app.models.user import User
from app.platform.request_context import set_request_user_id
from app.utils.logger import get_logger

logger = get_logger("auth.deps")

bearer_scheme = HTTPBearer(auto_error=False)

#: Name of the HttpOnly cookie the web client uses (set on auth responses). The
#: Authorization header still takes precedence — mobile keeps sending a bearer
#: token — so this is a pure, backward-compatible fallback for browsers.
AUTH_COOKIE = "loupe_token"


def _credentials(
    request: Request, creds: HTTPAuthorizationCredentials | None
) -> HTTPAuthorizationCredentials | None:
    """Prefer the Authorization header; fall back to the auth cookie (web)."""
    if creds is not None and creds.credentials:
        return creds
    token = request.cookies.get(AUTH_COOKIE)
    if token:
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    return None


async def _resolve_user(
    creds: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
    *,
    required: bool,
) -> User | None:
    if creds is None or not creds.credentials:
        if required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None
    try:
        claims = verify_token(creds.credentials, expected_type="access")
    except jwt.PyJWTError as exc:
        logger.info("Rejected JWT: %s", exc)
        if required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        return None
    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        if required:
            raise HTTPException(
                status_code=401, detail="Malformed subject claim"
            ) from exc
        return None
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None and required:
        raise HTTPException(status_code=401, detail="User not found")
    if user is not None and user.deleted_at is not None:
        if required:
            raise HTTPException(status_code=401, detail="User deactivated")
        return None
    if user is not None and user.banned_at is not None:
        if required:
            raise HTTPException(status_code=403, detail="Account suspended")
        return None
    # Token epoch: a token minted before the user's last "sign out everywhere"
    # (or password change) carries a stale `ver` and is rejected. Absent claim
    # defaults to 0 so tokens issued before this feature still validate.
    if user is not None and int(claims.get("ver", 0)) != user.token_version:
        if required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None
    if user is not None:
        # Stamp the user-id on the request context so structured logs
        # downstream (and Sentry events) can attribute requests to a user
        # without each handler having to thread it through manually.
        set_request_user_id(str(user.id))
    return user


async def require_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Dependency: a valid signed-in user, or HTTP 401."""
    user = await _resolve_user(_credentials(request, creds), db, required=True)
    assert user is not None
    return user


async def optional_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Dependency: user if present + valid, else ``None``."""
    return await _resolve_user(_credentials(request, creds), db, required=False)


def is_super_admin(user: User) -> bool:
    """Bootstrap super-admin — email is in the ``ADMIN_EMAILS`` env allowlist.

    Super-admins can't be demoted, banned, or deleted from the portal, so
    there's always a way back in even if the DB role flags get muddled.
    """
    allow = get_settings().admin_email_set
    email = (user.email or "").strip().lower()
    return bool(email) and email in allow


def is_admin_user(user: User) -> bool:
    """Effective admin: the DB-backed grant OR the env bootstrap allowlist."""
    return bool(getattr(user, "is_admin", False)) or is_super_admin(user)


async def require_admin(user: User = Depends(require_user)) -> User:
    """Dependency: a signed-in **admin** user (DB grant or env allowlist).

    Raises:
        HTTPException(403): when the user is neither flagged ``is_admin``
            nor in the ``ADMIN_EMAILS`` allowlist.
    """
    if not is_admin_user(user):
        logger.warning("admin-gate denied user=%s email=%s", user.id, user.email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


__all__ = [
    "AUTH_COOKIE",
    "bearer_scheme",
    "is_admin_user",
    "is_super_admin",
    "optional_user",
    "require_admin",
    "require_user",
]
