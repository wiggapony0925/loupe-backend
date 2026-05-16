"""Authentication subsystem (JWT, Apple/Google OIDC, FastAPI deps)."""

from app.auth.apple import AppleClaims, verify_apple_identity_token
from app.auth.dependencies import optional_user, require_user
from app.auth.google import GoogleClaims, verify_google_id_token
from app.auth.jwt import issue_token, verify_token

__all__ = [
    "AppleClaims",
    "GoogleClaims",
    "issue_token",
    "optional_user",
    "require_user",
    "verify_apple_identity_token",
    "verify_google_id_token",
    "verify_token",
]
