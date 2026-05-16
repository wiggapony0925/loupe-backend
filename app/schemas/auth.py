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


class AppleSignInRequest(BaseModel):
    "DevLoginRequest",
    "EmailSignInRequest",
    "EmailSignUpRequest",
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
    "GoogleSignInRequest",
    "RefreshRequest",
    "TokenPair",
]
