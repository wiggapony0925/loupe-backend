"""New-device sign-in notices — "was this you?".

The account-takeover signal a password-only breach can't hide: even if the
attacker has valid credentials, the real owner gets mail the moment a device
we've never seen signs in.

**Fingerprint = user-agent only, deliberately.** Folding the IP in would make
every coffee shop, every LTE↔Wi-Fi handoff, and every mobile carrier NAT look
like a new device, and an alert that cries wolf gets filtered to trash — which
costs more security than it buys. The IP is still *reported* in the email
(it's what a user checks to recognize a session); it just doesn't decide
whether the email fires.

Known devices live in ``kv_cache`` (shared Postgres, so the fleet agrees)
rather than the delivery log, because a device must be recorded as known even
on the paths where no mail is sent — a first sign-in right after signup, for
one. Six-month TTL: long enough that a regular user is never re-alerted for
the same laptop, short enough that a device retired a season ago re-alerts.
"""

from __future__ import annotations

import hashlib
import re

from app.models.user import User
from app.platform.cache_l2 import kv_get, kv_set
from app.utils.logger import get_logger

logger = get_logger("auth.device_notice")

#: How long a device stays "known" without being seen again.
_KNOWN_TTL_SECONDS = 180 * 24 * 60 * 60

#: A sign-in within this window of account creation is the signup itself —
#: record the device, stay silent. Nobody needs "new sign-in" 4 seconds after
#: choosing a password.
_SIGNUP_GRACE_SECONDS = 600

#: (needle, label) — first match wins, so order matters: "Edg" before
#: "Chrome" and "Chrome" before "Safari", since each impersonates the last.
_BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_PLATFORMS = (
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("Macintosh", "Mac"),
    ("Mac OS X", "Mac"),
    ("Windows", "Windows"),
    ("Linux", "Linux"),
)


def describe_device(user_agent: str | None) -> str:
    """A human label for the UA — 'iPhone · Safari', 'Windows · Chrome'.

    Falls back to the raw UA (truncated) so an unrecognized client still tells
    the user *something*; an empty UA becomes a clearly-unknown label rather
    than a blank line in the email.
    """
    if not user_agent:
        return "Unrecognized device"
    # Our own apps identify themselves; prefer that over UA archaeology.
    if match := re.match(r"^(Loupe[\w/.-]*)", user_agent):
        return match.group(1)
    platform = next((label for n, label in _PLATFORMS if n in user_agent), None)
    browser = next((label for n, label in _BROWSERS if n in user_agent), None)
    if platform and browser:
        return f"{platform} · {browser}"
    return platform or browser or user_agent[:60]


def _fingerprint(user_agent: str | None) -> str:
    return hashlib.sha256((user_agent or "unknown").encode("utf-8")).hexdigest()[:32]


def client_ip(headers: dict[str, str] | None, fallback: str | None) -> str | None:
    """Caller IP, honoring the proxy header Cloud Run sets.

    ``X-Forwarded-For`` is a chain (``client, proxy1, proxy2``); the leftmost
    entry is the original client.
    """
    forwarded = (headers or {}).get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return fallback


async def notify_if_new_device(
    user: User,
    *,
    user_agent: str | None,
    ip: str | None,
    account_age_seconds: float | None = None,
) -> bool:
    """Mark the device seen; email the owner if it wasn't. Never raises.

    Returns True when a notice was queued — sign-in must succeed regardless,
    so every failure here is swallowed and logged.
    """
    from app.services import email_service

    try:
        key = f"signin_device:{user.id}:{_fingerprint(user_agent)}"
        if await kv_get(key) is not None:
            return False
        await kv_set(key, "1", _KNOWN_TTL_SECONDS)
        if (
            account_age_seconds is not None
            and account_age_seconds < _SIGNUP_GRACE_SECONDS
        ):
            return False  # the signup itself — device recorded, no mail
        return await email_service.send_new_sign_in(
            user, device=describe_device(user_agent), ip=ip
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("new-device notice failed for user=%s (%s)", user.id, exc)
        return False


__all__ = ["client_ip", "describe_device", "notify_if_new_device"]
