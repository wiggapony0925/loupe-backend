"""Google ID-token verification.

Validates the token against Google's published JWKS, expected audience
(your OAuth client ID), and issuer ``accounts.google.com`` /
``https://accounts.google.com``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.cache_config import JWKS_CACHE_TTL
from app.clients.redis_client import get_redis
from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("auth.google")

GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_REDIS_KEY = "loupe:auth:google:jwks"

_inproc_jwks: dict[str, Any] | None = None
_inproc_jwks_at: float = 0.0


@dataclass(frozen=True)
class GoogleClaims:
    """Subset of Google ID-token claims we actually use."""

    sub: str
    email: str | None
    email_verified: bool
    name: str | None
    picture: str | None


async def _fetch_jwks() -> dict[str, Any]:
    """Fetch JWKS from Google, with Redis cache + in-proc fallback."""
    global _inproc_jwks, _inproc_jwks_at
    redis = await get_redis()
    if redis is not None:
        cached = await redis.get(_REDIS_KEY)
        if cached:
            return json.loads(cached)
    now = time.time()
    if _inproc_jwks is not None and now - _inproc_jwks_at < JWKS_CACHE_TTL:
        return _inproc_jwks
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
        resp = await client.get(GOOGLE_JWKS_URL)
        resp.raise_for_status()
        jwks: dict[str, Any] = resp.json()
    if redis is not None:
        await redis.setex(_REDIS_KEY, JWKS_CACHE_TTL, json.dumps(jwks))
    _inproc_jwks = jwks
    _inproc_jwks_at = now
    return jwks


def _select_key(jwks: dict[str, Any], kid: str) -> dict[str, Any]:
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    raise jwt.InvalidTokenError(f"No Google JWK matched kid={kid!r}")


async def verify_google_id_token(id_token: str) -> GoogleClaims:
    """Validate a Google ID token and return relevant claims."""
    s = get_settings()
    allowed_audiences = [
        a for a in (s.google_ios_client_id, s.google_web_client_id) if a
    ]
    if not allowed_audiences:
        raise RuntimeError(
            "No Google client IDs configured "
            "(set GOOGLE_IOS_CLIENT_ID and/or GOOGLE_WEB_CLIENT_ID)."
        )
    header = jwt.get_unverified_header(id_token)
    kid = header.get("kid")
    if not kid:
        raise jwt.InvalidTokenError("Google ID token is missing 'kid' header")
    jwks = await _fetch_jwks()
    key_dict = _select_key(jwks, kid)
    public_key = RSAAlgorithm.from_jwk(json.dumps(key_dict))
    payload: dict[str, Any] = jwt.decode(
        id_token,
        public_key,  # type: ignore[arg-type]
        algorithms=["RS256"],
        audience=allowed_audiences,
        options={"require": ["exp", "iat", "sub"]},
    )
    iss = payload.get("iss")
    if iss not in GOOGLE_ISSUERS:
        raise jwt.InvalidTokenError(f"Unexpected Google issuer: {iss!r}")
    return GoogleClaims(
        sub=str(payload["sub"]),
        email=payload.get("email"),
        email_verified=bool(payload.get("email_verified", False)),
        name=payload.get("name"),
        picture=payload.get("picture"),
    )


__all__ = ["GoogleClaims", "verify_google_id_token"]
