"""Profile-picture storage on the unified blob store (GCS in prod).

Avatars are small public images, so they skip the presigned-URL dance the
scan/report pipelines use: bytes live at a deterministic key and are served
straight through ``GET /v1/social/avatar/{user_id}`` with long cache headers.
`SocialProfile.avatar_version` bumps on every upload and rides the URL as
``?v=N``, so clients never see a stale cached picture.
"""

from __future__ import annotations

import uuid

from app.config import get_settings
from app.platform import blob_store
from app.social.models import SocialProfile

# Conservative caps: plenty for a square profile picture, small enough that
# the pass-through serving endpoint stays cheap.
MAX_AVATAR_BYTES = 5 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _key(user_id: uuid.UUID) -> str:
    return f"social/avatars/{user_id}"


def _bucket() -> str:
    """GCS bucket in prod (``GCS_BUCKET``), S3/stub bucket elsewhere."""
    s = get_settings()
    return s.gcs_bucket or s.s3_bucket


def avatar_url(profile: SocialProfile) -> str | None:
    """Cache-busted URL for a profile's picture (None if unset).

    Kept **relative** (`/v1/...`) on purpose: the web tier proxies ``/v1`` in
    every environment (vite proxy in dev, nginx in prod), so a relative URL
    works in ``<img>`` tags everywhere — including the mobile WebView embeds.
    A native client should resolve it against its own API base URL.
    """
    if not profile.avatar_key:
        return None
    return f"/v1/social/avatar/{profile.user_id}?v={profile.avatar_version}"


async def store_avatar(profile: SocialProfile, body: bytes, content_type: str) -> None:
    """Write the picture to the blob store and stamp the profile columns.

    Overwrites in place (one object per user) — the version bump, not the
    key, is what invalidates client caches. Caller commits.
    """
    key = _key(profile.user_id)
    await blob_store.put_object(
        bucket=_bucket(),
        key=key,
        body=body,
        content_type=content_type,
    )
    profile.avatar_key = key
    profile.avatar_content_type = content_type
    profile.avatar_version = (profile.avatar_version or 0) + 1


async def load_avatar(user_id: uuid.UUID) -> bytes | None:
    """Raw picture bytes for the serving endpoint (None = never uploaded)."""
    return await blob_store.get_object(bucket=_bucket(), key=_key(user_id))


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_AVATAR_BYTES",
    "avatar_url",
    "load_avatar",
    "store_avatar",
]
