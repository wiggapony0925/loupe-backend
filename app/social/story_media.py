"""Blob storage for story media.

Same shape as :mod:`app.social.post_media` — deterministic key off the row
id, served through a public endpoint — with a different prefix so a
lifecycle rule can be pointed at stories alone. Stories are the one thing
in this product that is *meant* to become garbage on a schedule; keeping
their objects under ``social/stories/`` means a bucket rule can reclaim
them without a query, and without any risk of catching a post's photo.

The size and type rules are shared with posts rather than re-stated:
a video the feed accepts and a story rejects (or the reverse) is a
difference nobody could explain, and the recording UI is the same camera.
"""

from __future__ import annotations

import uuid

from app.config import get_settings
from app.platform import blob_store
from app.social.post_media import (
    ALLOWED_CONTENT_TYPES,
    is_video,
    max_bytes,
    probe_size,
)

#: How long a story stays visible. The number everyone already expects.
STORY_TTL_HOURS = 24

#: Video length cap, enforced on the recording side and re-checked here.
#: Fifteen seconds is Instagram's per-card length; anything longer wants
#: splitting into cards rather than one long clip nobody taps through.
MAX_STORY_DURATION_MS = 15_000


def storage_key(story_id: uuid.UUID) -> str:
    return f"social/stories/{story_id}"


def media_url(story_id: uuid.UUID) -> str:
    """Relative URL for a story's media.

    Relative for the same reason avatars and post images are: the web tier
    proxies ``/v1`` in every environment, so one URL works in an ``<img>``,
    in the mobile WebView embeds, and — resolved against the API base — in
    a native player.
    """
    return f"/v1/social/stories/media/{story_id}"


def _bucket() -> str:
    s = get_settings()
    return s.gcs_bucket or s.s3_bucket


async def store(story_id: uuid.UUID, body: bytes, content_type: str) -> str:
    """Write one story's media and return its object key."""
    key = storage_key(story_id)
    await blob_store.put_object(
        bucket=_bucket(), key=key, body=body, content_type=content_type
    )
    return key


async def load(story_id: uuid.UUID) -> bytes | None:
    return await blob_store.get_object(bucket=_bucket(), key=storage_key(story_id))


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_STORY_DURATION_MS",
    "STORY_TTL_HOURS",
    "is_video",
    "load",
    "max_bytes",
    "media_url",
    "probe_size",
    "storage_key",
    "store",
]
