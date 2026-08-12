"""MFA enrollment + verification, orchestrated over the ``User`` model.

State machine:
    (none) --start_enrollment--> secret stored, mfa_enabled=False
           --confirm_enrollment(code)--> mfa_enabled=True, backup codes issued
           --disable(code)--> back to (none)

Sign-in calls :func:`verify_login` with a TOTP *or* a one-time backup code.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import mfa
from app.models.user import User


class MfaError(Exception):
    """Invalid MFA state transition or bad code — surfaced as 4xx by the router."""


async def start_enrollment(db: AsyncSession, user: User) -> dict[str, str]:
    """Generate (or regenerate) a pending secret and return enrollment material.

    Does not enable MFA — the user must confirm with a live code first.
    """
    secret = mfa.generate_secret()
    user.mfa_secret = mfa.seal_secret(secret)
    await db.commit()
    uri = mfa.provisioning_uri(secret, user.email)
    return {
        "secret": secret,
        "otpauth_uri": uri,
        "qr_svg": mfa.qr_svg_data_uri(uri),
    }


async def confirm_enrollment(db: AsyncSession, user: User, code: str) -> list[str]:
    """Verify the first code, flip MFA on, and return one-time backup codes."""
    if user.mfa_enabled:
        raise MfaError("Two-factor auth is already enabled.")
    secret = mfa.unseal_secret(user.mfa_secret)
    if not secret:
        raise MfaError("Start enrollment first.")
    if not mfa.verify_code(secret, code):
        raise MfaError("That code didn't match. Check the app and try again.")
    codes = mfa.generate_backup_codes()
    user.mfa_backup_codes = mfa.hash_backup_codes(codes)
    user.mfa_enabled = True
    user.mfa_enrolled_at = datetime.now(UTC)
    await db.commit()
    return codes


async def disable(db: AsyncSession, user: User, code: str) -> bool:
    """Turn MFA off after re-verifying a code (TOTP or backup).

    Returns True when this call actually turned the second factor off, and
    False when it was already off (the idempotent no-op). Callers use the
    return value to decide whether to raise a security alert — telling
    someone their 2FA was disabled when nothing changed is a false alarm on
    the one notice that must always be believed.
    """
    if not user.mfa_enabled:
        return False
    if not await verify_login(db, user, code):
        raise MfaError("That code didn't match.")
    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_backup_codes = None
    user.mfa_enrolled_at = None
    await db.commit()
    return True


async def verify_login(db: AsyncSession, user: User, code: str) -> bool:
    """True when ``code`` is a valid TOTP or an unused backup code.

    A matched backup code is consumed (removed) so it can't be replayed.
    """
    secret = mfa.unseal_secret(user.mfa_secret)
    if secret and mfa.verify_code(secret, code):
        return True
    matched, remaining = mfa.consume_backup_code(user.mfa_backup_codes, code)
    if matched:
        user.mfa_backup_codes = remaining
        await db.commit()
        return True
    return False


__all__ = [
    "MfaError",
    "confirm_enrollment",
    "disable",
    "start_enrollment",
    "verify_login",
]
