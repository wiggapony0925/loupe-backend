"""Signed one-click unsubscribe tokens for announcement email.

Every announcement message links to
``{api}/v1/public/unsubscribe?token=<uid>.<sig>`` where ``sig`` is an HMAC
over the user id, keyed off the JWT signing material (purpose-bound via
:func:`app.auth.jwt.derive_hmac_key`). No expiry — an unsubscribe link in a
year-old email must still work. The token can only flip
``email_announcements_enabled`` off, so replaying one is harmless.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import derive_hmac_key
from app.config import get_settings
from app.models.user import User, UserSettings
from app.utils.logger import get_logger

logger = get_logger("services.unsubscribe")

_PURPOSE = "email-unsubscribe-v1"
_SIG_HEX_LEN = 32  # 128 bits of the HMAC-SHA256 — plenty for a link token


def _sign(user_id: str) -> str:
    mac = hmac.new(derive_hmac_key(_PURPOSE), user_id.encode(), hashlib.sha256)
    return mac.hexdigest()[:_SIG_HEX_LEN]


def mint_token(user_id: str) -> str:
    """Opaque-ish unsubscribe token for one user: ``<uuid>.<sig>``."""
    return f"{user_id}.{_sign(user_id)}"


def unsubscribe_url(user_id: str) -> str:
    """Absolute one-click unsubscribe URL for email bodies + headers."""
    base = get_settings().api_base_url
    return f"{base}/v1/public/unsubscribe?token={mint_token(user_id)}"


def resolve_token(token: str) -> uuid.UUID | None:
    """Return the user id a valid token points at, else None."""
    user_id, sep, sig = token.partition(".")
    if not sep:
        return None
    if not hmac.compare_digest(sig, _sign(user_id)):
        return None
    try:
        return uuid.UUID(user_id)
    except ValueError:
        return None


async def apply_unsubscribe(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Turn announcement email off for ``user_id``. Idempotent; True if the
    user exists (already-unsubscribed still counts as success)."""
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        return False
    settings_row = (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one_or_none()
    if settings_row is None:
        settings_row = UserSettings(user_id=user_id)
        db.add(settings_row)
    settings_row.email_announcements_enabled = False
    await db.commit()
    logger.info("announcement email unsubscribed user=%s", user_id)
    return True


__all__ = [
    "apply_unsubscribe",
    "mint_token",
    "resolve_token",
    "unsubscribe_url",
]
