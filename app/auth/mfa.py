"""TOTP two-factor auth helpers.

Covers the whole MFA lifecycle without leaking the shared secret to logs or
the DB in the clear:

* secret generation + the ``otpauth://`` provisioning URI + a self-contained
  QR (SVG data-URI, rendered with pure-python ``segno`` — no external service),
* time-based code verification (with a one-step window for clock skew),
* one-time backup/recovery codes (stored only as argon2 hashes, consumed on use),
* at-rest *sealing* of the secret: Fernet-encrypted when ``MFA_SECRET_KEY`` is
  configured, otherwise stored with a ``p:`` prefix and a startup warning so
  dev/test still work.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import string

import pyotp
import segno
from cryptography.fernet import Fernet, InvalidToken

from app.auth.passwords import hash_password, verify_password
from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("auth.mfa")

_SEAL_FERNET = "f:"
_SEAL_PLAIN = "p:"
_CODE_ALPHABET = string.ascii_lowercase + string.digits


def _fernet() -> Fernet | None:
    """Build a Fernet from ``MFA_SECRET_KEY`` (a Fernet key, or any string we
    hash into one), or ``None`` when unset."""
    key = get_settings().mfa_secret_key.strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        # Not a valid Fernet key — derive a stable one from the provided secret.
        digest = hashlib.sha256(key.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def seal_secret(plain: str) -> str:
    """Encrypt a TOTP secret for storage (or mark it plaintext in dev).

    In production we refuse to persist an unencrypted TOTP secret: storing 2FA
    seeds in the clear would let a single DB read defeat every user's 2FA. New
    enrollments fail loudly until ``MFA_SECRET_KEY`` is configured.
    """
    fernet = _fernet()
    if fernet is None:
        if get_settings().is_production:
            raise RuntimeError(
                "MFA_SECRET_KEY is not set; refusing to store a TOTP secret "
                "unencrypted in production. Configure a Fernet key to enable 2FA."
            )
        logger.warning(
            "MFA_SECRET_KEY not set; storing TOTP secret unencrypted (dev only)"
        )
        return _SEAL_PLAIN + plain
    return _SEAL_FERNET + fernet.encrypt(plain.encode()).decode()


def unseal_secret(sealed: str | None) -> str | None:
    """Inverse of :func:`seal_secret`; ``None`` if it can't be recovered."""
    if not sealed:
        return None
    if sealed.startswith(_SEAL_PLAIN):
        return sealed[len(_SEAL_PLAIN) :]
    if sealed.startswith(_SEAL_FERNET):
        fernet = _fernet()
        if fernet is None:
            logger.error("MFA secret is encrypted but MFA_SECRET_KEY is unset")
            return None
        try:
            return fernet.decrypt(sealed[len(_SEAL_FERNET) :].encode()).decode()
        except InvalidToken:
            logger.error("Failed to decrypt TOTP secret (key rotated/mismatched?)")
            return None
    return sealed  # legacy/raw value


def generate_secret() -> str:
    """A fresh base32 TOTP shared secret."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, account: str) -> str:
    """The ``otpauth://`` URI an authenticator app enrolls from."""
    issuer = get_settings().mfa_issuer
    return pyotp.totp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def qr_svg_data_uri(uri: str) -> str:
    """A scannable QR for ``uri`` as an inline ``data:image/svg+xml`` URI."""
    qr = segno.make(uri, error="m")
    return qr.svg_data_uri(scale=4, border=2, dark="#000000", light="#ffffff")


def verify_code(secret: str | None, code: str | None) -> bool:
    """True when ``code`` is a valid TOTP for ``secret`` (±1 time step)."""
    if not secret or not code:
        return False
    cleaned = code.strip().replace(" ", "").replace("-", "")
    if not cleaned.isdigit():
        return False
    return bool(pyotp.totp.TOTP(secret).verify(cleaned, valid_window=1))


# ── Backup / recovery codes ───────────────────────────────────────────────


def _one_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def generate_backup_codes(n: int = 10) -> list[str]:
    """Fresh, human-typable one-time recovery codes (shown to the user once)."""
    return [_one_code() for _ in range(n)]


def hash_backup_codes(codes: list[str]) -> list[str]:
    """Argon2 hashes for storage — the plaintext codes are never persisted."""
    return [hash_password(c) for c in codes]


def consume_backup_code(
    hashes: list[str] | None, code: str | None
) -> tuple[bool, list[str]]:
    """Match ``code`` against the stored hashes.

    Returns ``(matched, remaining_hashes)``; on a match the used code's hash is
    dropped so it can't be replayed.
    """
    if not hashes or not code:
        return False, hashes or []
    cleaned = code.strip().lower().replace(" ", "")
    for i, h in enumerate(hashes):
        if verify_password(cleaned, h):
            return True, hashes[:i] + hashes[i + 1 :]
    return False, hashes


__all__ = [
    "consume_backup_code",
    "generate_backup_codes",
    "generate_secret",
    "hash_backup_codes",
    "provisioning_uri",
    "qr_svg_data_uri",
    "seal_secret",
    "unseal_secret",
    "verify_code",
]
