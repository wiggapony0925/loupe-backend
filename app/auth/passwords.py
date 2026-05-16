"""Argon2id password hashing + verification.

Argon2 is OWASP's current top recommendation. ``argon2-cffi`` is already in
``requirements.txt``; we use the default profile (memory_cost=65MB, time=3,
parallelism=4) which is safe for a 2026 server and resists GPU attacks.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    """Return an argon2id hash safe to store in the database."""
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, hashed: str | None) -> bool:
    """Constant-time verification. Returns ``False`` if ``hashed`` is empty."""
    if not hashed:
        return False
    try:
        return _hasher.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError):
        return False


def needs_rehash(hashed: str) -> bool:
    """True when the stored hash uses outdated parameters."""
    try:
        return _hasher.check_needs_rehash(hashed)
    except Exception:
        return False


__all__ = ["hash_password", "needs_rehash", "verify_password"]
