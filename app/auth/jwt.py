"""JWT issuance & verification (RS256).

In development, if no private key is configured, a per-process ephemeral RSA
key is generated so the API still works end-to-end without operator setup.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Literal

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("auth.jwt")

_lock = threading.Lock()
_ephemeral_private_pem: bytes | None = None
_ephemeral_public_pem: bytes | None = None

TokenType = Literal["access", "refresh"]


def _ensure_ephemeral_keys() -> tuple[bytes, bytes]:
    """Lazily generate an in-process RSA key pair for dev/test use."""
    global _ephemeral_private_pem, _ephemeral_public_pem
    with _lock:
        if _ephemeral_private_pem is None or _ephemeral_public_pem is None:
            logger.warning(
                "No JWT private key configured; generating ephemeral RSA key for this process"
            )
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            _ephemeral_private_pem = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            _ephemeral_public_pem = key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        return _ephemeral_private_pem, _ephemeral_public_pem


def _configured_pem(value: str) -> bytes | None:
    value = value.strip()
    if not value or value.startswith("#"):
        return None
    return value.replace("\\n", "\n").encode("utf-8")


def _private_key() -> bytes:
    s = get_settings()
    if pem := _configured_pem(s.jwt_private_key_pem):
        return pem
    return _ensure_ephemeral_keys()[0]


def _public_key() -> bytes:
    s = get_settings()
    if pem := _configured_pem(s.jwt_public_key_pem):
        return pem
    return _ensure_ephemeral_keys()[1]


def issue_token(
    user_id: uuid.UUID | str,
    token_type: TokenType = "access",
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Sign and return ``(jwt, ttl_seconds)`` for ``user_id``."""
    s = get_settings()
    ttl = (
        s.jwt_access_ttl_seconds
        if token_type == "access"
        else s.jwt_refresh_ttl_seconds
    )
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "sub": str(user_id),
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        "typ": token_type,
    }
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(payload, _private_key(), algorithm="RS256")
    return token, ttl


def issue_mfa_token(user_id: uuid.UUID | str) -> tuple[str, int]:
    """Sign a short-lived ``typ="mfa"`` token proving the password step passed.

    The client exchanges it (plus a TOTP/backup code) at ``/auth/mfa/verify``
    for a real access+refresh pair. Deliberately *not* an access token: it
    can't authenticate any API call on its own.
    """
    s = get_settings()
    ttl = s.jwt_mfa_ttl_seconds
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "sub": str(user_id),
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        "typ": "mfa",
    }
    token = jwt.encode(payload, _private_key(), algorithm="RS256")
    return token, ttl


def verify_token(token: str, expected_type: TokenType | None = None) -> dict[str, Any]:
    """Verify signature, audience, issuer, expiry.  Raise ``jwt.PyJWTError`` on failure."""
    s = get_settings()
    decoded: dict[str, Any] = jwt.decode(
        token,
        _public_key(),
        algorithms=["RS256"],
        audience=s.jwt_audience,
        issuer=s.jwt_issuer,
        # Allow small clock skew between this pod and the issuer pod.
        # Without it, a token whose ``iat`` is 1s in the future (or whose
        # ``exp`` just passed) raises ImmatureSignatureError / ExpiredSignatureError
        # the moment any node drifts — a constant low-grade source of
        # spurious 401s in multi-instance / load-balanced deployments.
        leeway=getattr(s, "jwt_leeway_seconds", 30),
        options={"require": ["exp", "iat", "sub"]},
    )
    if expected_type and decoded.get("typ") != expected_type:
        raise jwt.InvalidTokenError(
            f"Expected token typ={expected_type}, got {decoded.get('typ')}"
        )
    return decoded


__all__ = ["issue_mfa_token", "issue_token", "verify_token"]
