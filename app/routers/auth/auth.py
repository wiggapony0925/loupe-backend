"""Auth endpoints: email/password + Apple/Google sign-in + refresh."""

from __future__ import annotations

import jwt
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.apple import verify_apple_identity_token
from app.auth.dependencies import AUTH_COOKIE
from app.auth.google import verify_google_id_token
from app.auth.jwt import issue_token, verify_token
from app.config import get_settings
from app.db import get_db
from app.schemas.auth import (
    AppleSignInRequest,
    DevLoginRequest,
    EmailSignInRequest,
    EmailSignUpRequest,
    GoogleSignInRequest,
    RefreshRequest,
    TokenPair,
)
from app.schemas.user import UserRead
from app.services import email_service
from app.services.auth import user_service
from app.services.auth.user_service import EmailAlreadyExistsError
from app.utils.logger import get_logger

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger("routers.auth")


def _build_pair(user_id, user_read: UserRead) -> TokenPair:
    access, ttl = issue_token(user_id, "access")
    refresh, _ = issue_token(user_id, "refresh")
    # Effective admin = the DB grant (carried by model_validate) OR the env
    # bootstrap allowlist. Lets the client route admins straight to the
    # developer portal on sign-in (still enforced server-side on every call).
    email = (user_read.email or "").strip().lower()
    in_allowlist = bool(email) and email in get_settings().admin_email_set
    user_read.is_admin = bool(user_read.is_admin) or in_allowlist
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        token_type="bearer",
        expires_in=ttl,
        user=user_read,
    )


def _issue(response: Response, user_id, user_read: UserRead) -> TokenPair:
    """Build a TokenPair AND set the access token as an HttpOnly cookie.

    The body still carries the tokens (mobile reads them for its Bearer flow);
    the cookie is an additive, browser-only convenience so the web can move off
    JS-readable storage. `Secure` is on in production; `SameSite=Lax` is safe
    because the web calls the API same-origin (nginx proxies /v1).
    """
    pair = _build_pair(user_id, user_read)
    response.set_cookie(
        key=AUTH_COOKIE,
        value=pair.access_token,
        max_age=pair.expires_in,
        httponly=True,
        secure=get_settings().app_env == "production",
        samesite="lax",
        path="/",
    )
    return pair


@router.post(
    "/register",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    summary="Create account with email + password",
)
async def register(
    payload: EmailSignUpRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenPair:
    try:
        user = await user_service.create_with_password(
            db,
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
        )
    except EmailAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists.",
        ) from exc
    await email_service.send_welcome(user)  # best-effort; no-op without a provider
    return _issue(response, user.id, UserRead.model_validate(user))


@router.post(
    "/login",
    response_model=TokenPair,
    summary="Sign in with email + password",
)
async def login(
    payload: EmailSignInRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenPair:
    user = await user_service.authenticate_with_password(
        db, email=payload.email, password=payload.password
    )
    if user is None or user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    return _issue(response, user.id, UserRead.model_validate(user))


@router.post(
    "/dev-login",
    response_model=TokenPair,
    summary="Dev-only: sign in by email without a password",
)
async def dev_login(
    payload: DevLoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenPair:
    """Find-or-create a user by email, no password required.

    Gated by ``APP_ENV``: only available outside of ``production``.
    """
    if get_settings().app_env == "production":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    user = await user_service.find_or_create_dev_user(
        db, email=payload.email, display_name=payload.display_name
    )
    return _issue(response, user.id, UserRead.model_validate(user))


@router.post("/apple", response_model=TokenPair, summary="Sign in with Apple")
async def sign_in_with_apple(
    payload: AppleSignInRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenPair:
    try:
        claims = await verify_apple_identity_token(payload.identity_token)
    except (jwt.PyJWTError, RuntimeError) as exc:
        logger.info("Apple sign-in rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Apple identity token",
        ) from exc
    user = await user_service.find_or_create_by_apple(
        db,
        apple_sub=claims.sub,
        email=claims.email,
        display_name=payload.display_name,
    )
    return _issue(response, user.id, UserRead.model_validate(user))


@router.post("/google", response_model=TokenPair, summary="Sign in with Google")
async def sign_in_with_google(
    payload: GoogleSignInRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenPair:
    try:
        claims = await verify_google_id_token(payload.id_token)
    except (jwt.PyJWTError, RuntimeError) as exc:
        logger.info("Google sign-in rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google identity token",
        ) from exc
    user = await user_service.find_or_create_by_google(
        db,
        google_sub=claims.sub,
        email=claims.email,
        display_name=payload.display_name or claims.name,
        avatar_url=claims.picture,
    )
    return _issue(response, user.id, UserRead.model_validate(user))


@router.post("/refresh", response_model=TokenPair, summary="Refresh access token")
async def refresh(
    payload: RefreshRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenPair:
    try:
        claims = verify_token(payload.refresh_token, expected_type="refresh")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid refresh token") from exc
    import uuid as _uuid

    try:
        user_id = _uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Malformed token subject") from exc
    user = await user_service.get_by_id(db, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(status_code=401, detail="User not found")
    return _issue(response, user.id, UserRead.model_validate(user))


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear the auth cookie (web sign-out)",
)
async def logout(response: Response) -> None:
    """Expire the HttpOnly auth cookie. JS can't clear it, so the web calls this
    on sign-out. Bearer clients (mobile) simply drop their stored token."""
    response.delete_cookie(AUTH_COOKIE, path="/")


__all__ = ["router"]
