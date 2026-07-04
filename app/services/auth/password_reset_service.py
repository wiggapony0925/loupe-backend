"""Forgot-password flow — stateless, single-use reset tokens.

No DB columns: the token is ``{uid}.{expires_ts}.{sig}`` where ``sig`` is an
HMAC (purpose-bound key from the JWT signing material) over the user id, the
expiry, the user's ``token_version`` AND a fingerprint of the current password
hash. That binding makes tokens self-invalidating: using one bumps
``token_version`` and changes the hash, so the same link can never be replayed,
and requesting a new reset doesn't leave older links live past their expiry.

`request_reset` never reveals whether an account exists (the endpoint always
204s); social-only accounts get a "you sign in with Apple/Google" email
instead of a useless reset link.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import derive_hmac_key
from app.auth.passwords import hash_password
from app.config import get_settings
from app.models.user import User
from app.services import email_service
from app.services.auth import user_service
from app.utils.logger import get_logger

logger = get_logger("services.password_reset")

_PURPOSE = "password-reset-v1"
_SIG_HEX_LEN = 32
RESET_TTL_SECONDS = 30 * 60  # links die after 30 minutes


class ResetTokenError(Exception):
    """Invalid, expired, or already-used reset token."""


def _fingerprint(user: User) -> str:
    """Material that must be unchanged for a token to stay valid."""
    pw = user.password_hash or ""
    return f"{user.token_version or 0}|{hashlib.sha256(pw.encode()).hexdigest()[:16]}"


def _sign(user: User, expires_ts: int) -> str:
    msg = f"{user.id}|{expires_ts}|{_fingerprint(user)}"
    mac = hmac.new(derive_hmac_key(_PURPOSE), msg.encode(), hashlib.sha256)
    return mac.hexdigest()[:_SIG_HEX_LEN]


def mint_token(user: User) -> str:
    expires_ts = int(time.time()) + RESET_TTL_SECONDS
    return f"{user.id}.{expires_ts}.{_sign(user, expires_ts)}"


def reset_url(user: User) -> str:
    base = get_settings().app_public_url.rstrip("/")
    return f"{base}/reset-password?token={mint_token(user)}"


async def _resolve(db: AsyncSession, token: str) -> User:
    try:
        uid_s, exp_s, sig = token.split(".", 2)
        uid = uuid.UUID(uid_s)
        expires_ts = int(exp_s)
    except (ValueError, AttributeError) as exc:
        raise ResetTokenError("malformed") from exc
    if time.time() > expires_ts:
        raise ResetTokenError("expired")
    user = await user_service.get_by_id(db, uid)
    if user is None or user.deleted_at is not None or user.banned_at is not None:
        raise ResetTokenError("unknown user")
    if not hmac.compare_digest(sig, _sign(user, expires_ts)):
        raise ResetTokenError("bad signature")
    return user


async def request_reset(db: AsyncSession, email: str) -> None:
    """Email a reset link if the account exists. Silent otherwise —
    the caller must respond identically either way (no enumeration)."""
    user = await user_service.get_by_email(db, email)
    if user is None or user.deleted_at is not None or user.banned_at is not None:
        logger.info("password reset requested for unknown/inactive email")
        return
    if not user.password_hash:
        # Apple/Google-only account: a reset link would dead-end, but total
        # silence reads as a broken product to the legitimate owner.
        await email_service.send_reset_unavailable(user)
        return
    await email_service.send_password_reset(user, reset_url(user))


async def perform_reset(db: AsyncSession, token: str, new_password: str) -> User:
    """Set a new password from a valid token. Raises :class:`ResetTokenError`.

    Bumps ``token_version`` — which revokes every outstanding session AND
    retires the token itself (its signature covers the old version).
    """
    user = await _resolve(db, token)
    user.password_hash = hash_password(new_password)
    user.token_version = (user.token_version or 0) + 1
    await db.commit()
    await db.refresh(user)
    logger.info("password reset completed for user=%s", user.id)
    await email_service.send_password_changed(user)  # best-effort notice
    return user


__all__ = [
    "RESET_TTL_SECONDS",
    "ResetTokenError",
    "mint_token",
    "perform_reset",
    "request_reset",
    "reset_url",
]
