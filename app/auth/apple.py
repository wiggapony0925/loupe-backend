"""Sign-in-with-Apple identity-token verification.

Fetches Apple's JWKS, caches them in Redis (1h), validates RS256 signature,
issuer ``https://appleid.apple.com`` and the configured audience (your bundle id).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.platform.cache_config import JWKS_CACHE_TTL
from app.platform.redis_client import get_redis
from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("auth.apple")

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
_REDIS_KEY = "loupe:auth:apple:jwks"

_inproc_jwks: dict[str, Any] | None = None
_inproc_jwks_at: float = 0.0


@dataclass(frozen=True)
class AppleClaims:
    """Subset of Apple identity-token claims we actually use."""

    sub: str
    email: str | None
    email_verified: bool


async def _fetch_jwks() -> dict[str, Any]:
    """Fetch JWKS from Apple, with Redis cache + in-proc fallback."""
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
        resp = await client.get(APPLE_JWKS_URL)
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
    raise jwt.InvalidTokenError(f"No Apple JWK matched kid={kid!r}")


async def verify_apple_identity_token(identity_token: str) -> AppleClaims:
    """Validate an Apple identity token and return the relevant claims."""
    s = get_settings()
    if not s.apple_client_id:
        raise RuntimeError(
            "APPLE_CLIENT_ID is not configured; cannot verify Apple sign-in tokens."
        )
    header = jwt.get_unverified_header(identity_token)
    kid = header.get("kid")
    if not kid:
        raise jwt.InvalidTokenError("Apple identity token is missing 'kid' header")
    jwks = await _fetch_jwks()
    key_dict = _select_key(jwks, kid)
    public_key = RSAAlgorithm.from_jwk(json.dumps(key_dict))
    payload: dict[str, Any] = jwt.decode(
        identity_token,
        public_key,  # type: ignore[arg-type]
        algorithms=["RS256"],
        audience=s.apple_client_id,
        issuer=APPLE_ISSUER,
        options={"require": ["exp", "iat", "sub"]},
    )
    return AppleClaims(
        sub=str(payload["sub"]),
        email=payload.get("email"),
        email_verified=bool(payload.get("email_verified", False)),
    )


__all__ = ["AppleClaims", "verify_apple_identity_token"]
