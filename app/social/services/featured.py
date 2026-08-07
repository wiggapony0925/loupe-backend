"""Operator-curated featured collectors.

The Community rail used to be purely algorithmic (most-followed, then
biggest collection). That is a reasonable default and a bad ceiling: it
cannot promote a new collector worth seeing, and it cannot demote someone
who happens to have followers.

So the list is now CURATED when an operator says so and algorithmic when
they don't. The curation lives in ``kv_cache`` rather than a column — same
call as the carousel registry: operators change this from the admin portal
several times a week, and that should never need a migration or a deploy.

Stored as an ordered list of usernames, because that is what an operator
thinks in and types. Ids would be stable but unreadable in the admin UI and
impossible to enter by hand.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.platform.cache_l2 import kv_get, kv_set
from app.social.models import SocialProfile
from app.utils.logger import get_logger

logger = get_logger("social.featured")

KEY = "social:featured:v1"
#: No expiry in practice — curation is operator state, not a cache. The L2
#: API requires a TTL, so this is "ten years".
TTL_S = 10 * 365 * 24 * 3600
#: Guard rail: a rail is a browse shelf, not a directory.
MAX_FEATURED = 20


async def featured_usernames() -> list[str]:
    """The curated handles, in operator order. Empty = fall back to ranking."""
    raw = await kv_get(KEY)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("featured list is not valid JSON; ignoring")
        return []
    if not isinstance(value, list):
        return []
    return [str(v).strip().lower() for v in value if str(v).strip()]


async def set_featured(usernames: list[str]) -> list[str]:
    """Replace the list. De-duplicated, lower-cased, capped, order kept."""
    seen: list[str] = []
    for name in usernames:
        handle = str(name).strip().lower().lstrip("@")
        if handle and handle not in seen:
            seen.append(handle)
    seen = seen[:MAX_FEATURED]
    await kv_set(KEY, json.dumps(seen), ttl_seconds=TTL_S)
    return seen


async def add_featured(username: str) -> list[str]:
    current = await featured_usernames()
    return await set_featured([*current, username])


async def remove_featured(username: str) -> list[str]:
    handle = username.strip().lower().lstrip("@")
    return await set_featured([u for u in await featured_usernames() if u != handle])


async def resolve_featured(
    db: AsyncSession, handles: list[str]
) -> list[tuple[SocialProfile, User]]:
    """Profiles for the curated handles, IN THE OPERATOR'S ORDER.

    Handles that no longer resolve (renamed, deactivated, deleted, banned)
    are skipped rather than erroring: a stale entry must never be able to
    break the Community page for everyone. The admin view reports them so
    they can be cleaned up.
    """
    if not handles:
        return []
    rows = (
        await db.execute(
            select(SocialProfile, User)
            .join(User, User.id == SocialProfile.user_id)
            .where(
                SocialProfile.username.in_(handles),
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
            )
        )
    ).all()
    by_handle = {p.username: (p, u) for p, u in rows}
    return [by_handle[h] for h in handles if h in by_handle]


__all__ = [
    "MAX_FEATURED",
    "add_featured",
    "featured_usernames",
    "remove_featured",
    "resolve_featured",
    "set_featured",
]
