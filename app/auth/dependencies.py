"""FastAPI dependencies for authenticated request handling."""

from __future__ import annotations

import uuid

import jwt
from fastapi import Depends, HTTPException, status
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
    if user is not None:
        # Stamp the user-id on the request context so structured logs
        # downstream (and Sentry events) can attribute requests to a user
        # without each handler having to thread it through manually.
        set_request_user_id(str(user.id))
    return user


async def require_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Dependency: a valid signed-in user, or HTTP 401."""
    user = await _resolve_user(creds, db, required=True)
    assert user is not None
    return user


async def optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Dependency: user if present + valid, else ``None``."""
    return await _resolve_user(creds, db, required=False)


async def require_admin(user: User = Depends(require_user)) -> User:
    """Dependency: a signed-in user whose email is in the admin allowlist.

    Authorization model is intentionally minimal — until a proper role
    table lands on :class:`User`, admin membership is driven by the
    ``ADMIN_EMAILS`` env var (comma-separated). This keeps internal
    metrics / dashboards behind a real auth check rather than a
    TODO-stub, and is easy to ratchet up later without touching call
    sites.

    Raises:
        HTTPException(403): when the user's email isn't in the allowlist
            (or the allowlist is empty, which deliberately denies all).
    """
    allow = get_settings().admin_email_set
    email = (user.email or "").strip().lower()
    if not email or email not in allow:
        logger.warning(
            "admin-gate denied user=%s email=%s allowlist_size=%d",
            user.id,
            email or "<unset>",
            len(allow),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


__all__ = ["bearer_scheme", "optional_user", "require_admin", "require_user"]
