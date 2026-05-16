"""Security primitives: API-key hashing, secure random, password hashing."""

from __future__ import annotations

import hashlib
import hmac
import secrets


def secure_random(nbytes: int = 32) -> str:
    """Return a URL-safe random token with at least *nbytes* of entropy."""
    return secrets.token_urlsafe(nbytes)


def hash_api_key(raw_key: str) -> str:
    """Return a hex SHA-256 digest of an API key for safe at-rest storage."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def verify_api_key(raw_key: str, stored_hash: str) -> bool:
    """Constant-time compare of an API key against its stored SHA-256 hash."""
    return hmac.compare_digest(hash_api_key(raw_key), stored_hash)


def hash_password(password: str) -> str:
    """Hash a password using argon2id (falls back to sha256 if argon2 missing)."""
    try:
        from argon2 import PasswordHasher

        return PasswordHasher().hash(password)
    except Exception:
        return "sha256$" + hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, stored: str) -> bool:
    """Verify a password against an argon2 (or sha256-fallback) hash."""
    if stored.startswith("sha256$"):
        return hmac.compare_digest(
            stored, "sha256$" + hashlib.sha256(password.encode("utf-8")).hexdigest()
        )
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError

        try:
            return PasswordHasher().verify(stored, password)
        except VerifyMismatchError:
            return False
    except Exception:
        return False


__all__ = [
    "hash_api_key",
    "hash_password",
    "secure_random",
    "verify_api_key",
    "verify_password",
]
