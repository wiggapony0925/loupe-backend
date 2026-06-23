"""Auth schemas — email/password + SSO (Apple / Google) + token pair."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.user import UserRead


class EmailSignUpRequest(BaseModel):
    """Body for ``POST /v1/auth/register`` (email + password)."""

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str | None = Field(None, max_length=120)


class EmailSignInRequest(BaseModel):
    """Body for ``POST /v1/auth/login`` (email + password)."""

    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class DevLoginRequest(BaseModel):
    """Body for ``POST /v1/auth/dev-login`` (dev/test only, no password)."""

    email: EmailStr
    display_name: str | None = Field(None, max_length=120)


class TokenPair(BaseModel):
    """Returned by every successful sign-in / refresh."""

    model_config = ConfigDict(from_attributes=True)

    access_token: str = Field(..., description="Short-lived JWT bearer token.")
    refresh_token: str = Field(..., description="Long-lived refresh token.")
    token_type: str = Field("bearer", description="OAuth-style token type.")
    expires_in: int = Field(..., ge=1, description="Access token lifetime, seconds.")
    user: UserRead


class LoginResult(BaseModel):
    """Response for ``POST /v1/auth/login``.

    Backward-compatible superset of :class:`TokenPair`: on a normal sign-in the
    token fields are populated exactly as before (``mfa_required`` is ``False``).
    When the account has two-factor enabled, the tokens are withheld and
    ``mfa_required``/``mfa_token`` drive a second step at ``/auth/mfa/verify``.
    """

    model_config = ConfigDict(from_attributes=True)

    access_token: str | None = Field(
        default=None, description="Short-lived JWT bearer token."
    )
    refresh_token: str | None = Field(
        default=None, description="Long-lived refresh token."
    )
    token_type: str = Field(default="bearer", description="OAuth-style token type.")
    expires_in: int | None = Field(
        default=None, description="Access token lifetime, seconds."
    )
    user: UserRead | None = None
    mfa_required: bool = Field(
        default=False, description="True when a second factor is needed."
    )
    mfa_token: str | None = Field(
        default=None,
        description="Short-lived token to POST to /auth/mfa/verify with a code.",
    )


class MfaVerifyRequest(BaseModel):
    """Body for ``POST /v1/auth/mfa/verify`` (login second step)."""

    mfa_token: str = Field(..., min_length=10)
    code: str = Field(..., min_length=4, max_length=16)


class MfaCodeRequest(BaseModel):
    """Body for enable/disable — a single TOTP or backup code."""

    code: str = Field(..., min_length=4, max_length=16)


class MfaSetupResponse(BaseModel):
    """Enrollment material returned by ``POST /v1/auth/mfa/setup``."""

    secret: str = Field(..., description="Base32 TOTP secret (for manual entry).")
    otpauth_uri: str = Field(..., description="otpauth:// URI for authenticator apps.")
    qr_svg: str = Field(..., description="Inline data:image/svg+xml QR of the URI.")


class MfaEnableResponse(BaseModel):
    """One-time backup codes, shown once after enabling MFA."""

    backup_codes: list[str]


class MfaStatusResponse(BaseModel):
    """Whether the signed-in user has two-factor enabled."""

    enabled: bool


class AppleSignInRequest(BaseModel):
    """Body for ``POST /v1/auth/apple``."""

    identity_token: str = Field(..., min_length=10, description="JWT from Apple SDK.")
    nonce: str | None = Field(None, description="Original nonce, if used.")
    display_name: str | None = Field(None, max_length=120)


class GoogleSignInRequest(BaseModel):
    """Body for ``POST /v1/auth/google``."""

    id_token: str = Field(..., min_length=10, description="ID token from Google SDK.")
    display_name: str | None = Field(None, max_length=120)


class RefreshRequest(BaseModel):
    """Body for ``POST /v1/auth/refresh``."""

    refresh_token: str = Field(..., min_length=10)


__all__ = [
    "AppleSignInRequest",
    "DevLoginRequest",
    "EmailSignInRequest",
    "EmailSignUpRequest",
    "GoogleSignInRequest",
    "LoginResult",
    "MfaCodeRequest",
    "MfaEnableResponse",
    "MfaSetupResponse",
    "MfaStatusResponse",
    "MfaVerifyRequest",
    "RefreshRequest",
    "TokenPair",
]
