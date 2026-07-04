"""Email-address verification — signed links, same shape as unsubscribe.

Token is ``{uid}.{sig}`` (HMAC over the user id with a purpose-bound key).
Deliberately no expiry: the link can only ever mark *that* account's email as
verified, so a year-old welcome email still works and replay is harmless.
Verification is a trust signal — nothing is gated on it yet.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import derive_hmac_key
from app.config import get_settings
from app.models.user import User
from app.utils.logger import get_logger

logger = get_logger("services.email_verify")

_PURPOSE = "email-verify-v1"
_SIG_HEX_LEN = 32


def _sign(user_id: str) -> str:
    mac = hmac.new(derive_hmac_key(_PURPOSE), user_id.encode(), hashlib.sha256)
    return mac.hexdigest()[:_SIG_HEX_LEN]


def mint_token(user_id: str) -> str:
    return f"{user_id}.{_sign(user_id)}"


def verify_url(user_id: str) -> str:
    """Absolute confirmation URL for email bodies (hits the API directly)."""
    base = get_settings().api_base_url
    return f"{base}/v1/public/verify-email?token={mint_token(user_id)}"


def resolve_token(token: str) -> uuid.UUID | None:
    user_id, sep, sig = token.partition(".")
    if not sep or not hmac.compare_digest(sig, _sign(user_id)):
        return None
    try:
        return uuid.UUID(user_id)
    except ValueError:
        return None


async def apply_verification(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Mark ``user_id``'s email verified. Idempotent; True if the user exists."""
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        return False
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)
        await db.commit()
        logger.info("email verified for user=%s", user_id)
    return True


__all__ = ["apply_verification", "mint_token", "resolve_token", "verify_url"]
