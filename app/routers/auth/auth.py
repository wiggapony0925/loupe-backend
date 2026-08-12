"""Auth endpoints: email/password + Apple/Google sign-in + refresh."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.apple import verify_apple_identity_token
from app.auth.dependencies import AUTH_COOKIE, require_user
from app.auth.google import verify_google_id_token
from app.auth.jwt import issue_mfa_token, issue_token, verify_token
from app.config import get_settings
from app.db import get_db
from app.models.user import User
from app.platform.rate_limit import (
    change_password_limit,
    forgot_password_limit,
    login_limit,
    mfa_verify_limit,
    reset_password_limit,
)
from app.schemas.auth import (
    AppleSignInRequest,
    ChangePasswordRequest,
    DevLoginRequest,
    EmailSignInRequest,
    EmailSignUpRequest,
    ForgotPasswordRequest,
    GoogleSignInRequest,
    LoginResult,
    MfaCodeRequest,
    MfaEnableResponse,
    MfaSetupResponse,
    MfaStatusResponse,
    MfaVerifyRequest,
    RefreshRequest,
    ResetPasswordRequest,
    TokenPair,
)
from app.schemas.user import UserRead
from app.services import email_service
from app.services.auth import (
    device_notice_service,
    email_verify_service,
    mfa_service,
    password_reset_service,
    user_service,
)
from app.services.auth.mfa_service import MfaError
from app.services.auth.user_service import AccountLockedError, EmailAlreadyExistsError
from app.utils.logger import get_logger

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger("routers.auth")

#: The only environments where the passwordless ``/auth/dev-login`` back door
#: exists. Anything not listed here — production AND staging — 404s.
_DEV_LOGIN_ENVS: frozenset[str] = frozenset({"development", "test"})


def _build_pair(user_id, user_read: UserRead, *, token_version: int = 0) -> TokenPair:
    # `ver` pins each token to the user's current token epoch; bumping
    # `User.token_version` (e.g. /auth/logout-all) invalidates every token that
    # carries an older value. See app.auth.dependencies for the check.
    claims = {"ver": token_version}
    access, ttl = issue_token(user_id, "access", claims)
    refresh, _ = issue_token(user_id, "refresh", claims)
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


def _issue(
    response: Response, user_id, user_read: UserRead, *, token_version: int = 0
) -> TokenPair:
    """Build a TokenPair AND set the access token as an HttpOnly cookie.

    The body still carries the tokens (mobile reads them for its Bearer flow);
    the cookie is an additive, browser-only convenience so the web can move off
    JS-readable storage. `Secure` is on in production; `SameSite=Lax` is safe
    because the web calls the API same-origin (nginx proxies /v1).
    """
    pair = _build_pair(user_id, user_read, token_version=token_version)
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
    # Best-effort; no-op without a provider. Password signups haven't proven
    # the address, so the welcome carries the confirm-email CTA.
    await email_service.send_welcome(
        user, verify_url=email_verify_service.verify_url(str(user.id))
    )
    return _issue(
        response,
        user.id,
        UserRead.model_validate(user),
        token_version=user.token_version,
    )


@router.post(
    "/login",
    response_model=LoginResult,
    summary="Sign in with email + password",
)
async def login(
    payload: EmailSignInRequest,
    request: Request,
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
    except user_service.SsoOnlyAccountError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "This account signs in with Apple or Google — use those "
                "buttons. To add a password, use “Forgot password?”."
            ),
        ) from exc
    if user is None or user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    # Password OK. If the account has two-factor enabled, withhold the tokens
    # and hand back a short-lived challenge for /auth/mfa/verify.
    if user.mfa_enabled:
        # Not signed in yet — the device notice belongs on /mfa/verify, where
        # a session actually gets issued.
        mfa_token, _ = issue_mfa_token(user.id)
        return LoginResult(mfa_required=True, mfa_token=mfa_token)
    pair = _issue(
        response,
        user.id,
        UserRead.model_validate(user),
        token_version=user.token_version,
    )
    await _notify_device(request, user)
    return LoginResult.model_validate(pair, from_attributes=True)


async def _notify_device(request: Request, user: User) -> None:
    """Fire the new-device notice for a session that was just issued."""
    created = getattr(user, "created_at", None)
    age: float | None = None
    if created is not None:
        # SQLite drops tzinfo on round-trip; treat naive timestamps as UTC.
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - created).total_seconds()
    await device_notice_service.notify_if_new_device(
        user,
        user_agent=request.headers.get("user-agent"),
        ip=device_notice_service.client_ip(
            dict(request.headers), request.client.host if request.client else None
        ),
        account_age_seconds=age,
    )


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

    Gated by ``APP_ENV``: available ONLY in ``development`` and ``test``.
    Allow-list, not deny-list — staging serves real accounts over the public
    internet, so a passwordless route with no allowlist and no rate limit is
    account takeover for anyone who knows an email address, and any env added
    to the ``app_env`` Literal later must opt in deliberately.
    """
    if get_settings().app_env not in _DEV_LOGIN_ENVS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    user = await user_service.find_or_create_dev_user(
        db, email=payload.email, display_name=payload.display_name
    )
    return _issue(
        response,
        user.id,
        UserRead.model_validate(user),
        token_version=user.token_version,
    )


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
    return _issue(
        response,
        user.id,
        UserRead.model_validate(user),
        token_version=user.token_version,
    )


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
    return _issue(
        response,
        user.id,
        UserRead.model_validate(user),
        token_version=user.token_version,
    )


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
    # A banned user must not be able to mint fresh access tokens by refreshing.
    if user.banned_at is not None:
        raise HTTPException(status_code=403, detail="Account suspended")
    # Reject a refresh token from before the user's last revocation epoch.
    if int(claims.get("ver", 0)) != user.token_version:
        raise HTTPException(status_code=401, detail="Session has been revoked")
    return _issue(
        response,
        user.id,
        UserRead.model_validate(user),
        token_version=user.token_version,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear the auth cookie (web sign-out)",
)
async def logout(response: Response) -> None:
    """Expire the HttpOnly auth cookie. JS can't clear it, so the web calls this
    on sign-out. Bearer clients (mobile) simply drop their stored token."""
    response.delete_cookie(AUTH_COOKIE, path="/")


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke every session for the signed-in user",
)
async def logout_all(
    response: Response,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Bump the user's token epoch, instantly invalidating *all* of their
    outstanding access + refresh tokens (every device). The kill switch for a
    lost device or a stolen token. Also clears this browser's cookie."""
    await user_service.bump_token_version(db, user)
    response.delete_cookie(AUTH_COOKIE, path="/")


@router.post(
    "/change-password",
    response_model=TokenPair,
    summary="Change password (revokes other sessions)",
)
async def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    _throttle: None = Depends(change_password_limit),
) -> TokenPair:
    """Set a new password after verifying the current one. Bumps the token epoch
    so every *other* session is revoked, then re-issues a fresh pair for this
    device — so the user stays signed in here but is signed out everywhere else.
    """
    try:
        updated = await user_service.change_password(
            db,
            user,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except user_service.PasswordChangeError as exc:
        if exc.reason == "no_password":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This account signs in with Apple or Google — there's no password to change.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        ) from exc
    # Security notice — if an attacker changed it, this is how the real
    # owner finds out. Best-effort; no-op without a provider.
    await email_service.send_password_changed(updated)
    return _issue(
        response,
        updated.id,
        UserRead.model_validate(updated),
        token_version=updated.token_version,
    )


# ── Forgot / reset password ───────────────────────────────────────────────


@router.post(
    "/forgot-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Email a password-reset link (always 204)",
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
    _throttle: None = Depends(forgot_password_limit),
) -> None:
    """Request a reset link. Responds 204 whether or not the account exists,
    so the endpoint can't be used to enumerate emails."""
    await password_reset_service.request_reset(db, payload.email)


@router.post(
    "/reset-password",
    response_model=TokenPair,
    summary="Set a new password from an emailed reset token",
)
async def reset_password(
    payload: ResetPasswordRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _throttle: None = Depends(reset_password_limit),
) -> TokenPair:
    """Complete the reset. The token is single-use and revokes every other
    session; a fresh token pair signs this device straight in."""
    try:
        user = await password_reset_service.perform_reset(
            db, payload.token, payload.new_password
        )
    except password_reset_service.ResetTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That reset link is invalid or has expired. Request a new one.",
        ) from exc
    return _issue(
        response,
        user.id,
        UserRead.model_validate(user),
        token_version=user.token_version,
    )


# ── Two-factor auth (TOTP) ────────────────────────────────────────────────


@router.post(
    "/mfa/verify",
    response_model=TokenPair,
    summary="Complete sign-in with a 2FA code (login second step)",
)
async def mfa_verify(
    payload: MfaVerifyRequest,
    request: Request,
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
    pair = _issue(
        response,
        user.id,
        UserRead.model_validate(user),
        token_version=user.token_version,
    )
    await _notify_device(request, user)
    return pair


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
    await email_service.send_mfa_enabled(user)  # best-effort security notice
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
        turned_off = await mfa_service.disable(db, user, payload.code)
    except MfaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Only alert when the second factor actually came off. The call is
    # idempotent (204 either way), so mailing on the no-op both cries wolf on
    # the account's most important security notice and lets anyone holding a
    # session loop the endpoint to send unbounded mail from our domain.
    if turned_off:
        await email_service.send_mfa_disabled(user)  # best-effort security notice


__all__ = ["router"]
