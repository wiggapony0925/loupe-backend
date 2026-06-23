"""Auth endpoints: email/password + Apple/Google sign-in + refresh."""

from __future__ import annotations

import uuid

import jwt
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.apple import verify_apple_identity_token
from app.auth.dependencies import AUTH_COOKIE, require_user
from app.auth.google import verify_google_id_token
from app.auth.jwt import issue_mfa_token, issue_token, verify_token
from app.config import get_settings
from app.db import get_db
from app.models.user import User
from app.platform.rate_limit import login_limit, mfa_verify_limit
from app.schemas.auth import (
    AppleSignInRequest,
    DevLoginRequest,
    EmailSignInRequest,
    EmailSignUpRequest,
    GoogleSignInRequest,
    LoginResult,
    MfaCodeRequest,
    MfaEnableResponse,
    MfaSetupResponse,
    MfaStatusResponse,
    MfaVerifyRequest,
    RefreshRequest,
    TokenPair,
)
from app.schemas.user import UserRead
from app.services import email_service
from app.services.auth import mfa_service, user_service
from app.services.auth.mfa_service import MfaError
from app.services.auth.user_service import AccountLockedError, EmailAlreadyExistsError
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
    response_model=LoginResult,
    summary="Sign in with email + password",
)
async def login(
    payload: EmailSignInRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _throttle: None = Depends(login_limit),
) -> LoginResult:
    try:
        user = await user_service.authenticate_with_password(
            db, email=payload.email, password=payload.password
        )
    except AccountLockedError as exc:
        # Too many failed attempts — refuse for a cooling-off window.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again later.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    if user is None or user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    # Password OK. If the account has two-factor enabled, withhold the tokens
    # and hand back a short-lived challenge for /auth/mfa/verify.
    if user.mfa_enabled:
        mfa_token, _ = issue_mfa_token(user.id)
        return LoginResult(mfa_required=True, mfa_token=mfa_token)
    pair = _issue(response, user.id, UserRead.model_validate(user))
    return LoginResult.model_validate(pair, from_attributes=True)


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


# ── Two-factor auth (TOTP) ────────────────────────────────────────────────


@router.post(
    "/mfa/verify",
    response_model=TokenPair,
    summary="Complete sign-in with a 2FA code (login second step)",
)
async def mfa_verify(
    payload: MfaVerifyRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _throttle: None = Depends(mfa_verify_limit),
) -> TokenPair:
    """Exchange the login ``mfa_token`` + a TOTP/backup code for a token pair."""
    try:
        claims = verify_token(payload.mfa_token)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired sign-in. Start again.",
        ) from exc
    if claims.get("typ") != "mfa":
        raise HTTPException(status_code=401, detail="Invalid sign-in token")
    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Malformed token subject") from exc
    user = await user_service.get_by_id(db, user_id)
    if user is None or user.deleted_at is not None or not user.mfa_enabled:
        raise HTTPException(status_code=401, detail="Invalid sign-in")
    if not await mfa_service.verify_login(db, user, payload.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That code didn't match.",
        )
    return _issue(response, user.id, UserRead.model_validate(user))


@router.get(
    "/mfa/status",
    response_model=MfaStatusResponse,
    summary="Whether two-factor is enabled for the signed-in user",
)
async def mfa_status(user: User = Depends(require_user)) -> MfaStatusResponse:
    return MfaStatusResponse(enabled=bool(user.mfa_enabled))


@router.post(
    "/mfa/setup",
    response_model=MfaSetupResponse,
    summary="Begin 2FA enrollment — returns a secret + QR",
)
async def mfa_setup(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> MfaSetupResponse:
    if user.mfa_enabled:
        raise HTTPException(status_code=409, detail="Two-factor is already enabled.")
    material = await mfa_service.start_enrollment(db, user)
    return MfaSetupResponse(**material)


@router.post(
    "/mfa/enable",
    response_model=MfaEnableResponse,
    summary="Confirm enrollment with a code; returns one-time backup codes",
)
async def mfa_enable(
    payload: MfaCodeRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> MfaEnableResponse:
    try:
        codes = await mfa_service.confirm_enrollment(db, user, payload.code)
    except MfaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MfaEnableResponse(backup_codes=codes)


@router.post(
    "/mfa/disable",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disable 2FA after re-verifying a code",
)
async def mfa_disable(
    payload: MfaCodeRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await mfa_service.disable(db, user, payload.code)
    except MfaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


__all__ = ["router"]
