"""Feed-image storage on the unified blob store (GCS in prod).

Same shape as :mod:`app.social.avatars` — deterministic key, served straight
through a public endpoint with immutable cache headers — with one difference
that matters: a post image's key is keyed on the MEDIA row's id, not the
post's. Slides can be added, reordered or removed, so a key derived from
``post_id + position`` would let a reordered carousel serve last week's
picture out of a CDN cache. A per-row id is written once and never reused.

Images are public because the URL is unguessable and the endpoint is the
same trust level as the avatar one; the *post* (caption, author, comments)
stays behind the privacy gate in the feed service.
"""

from __future__ import annotations

import struct
import uuid

from app.config import get_settings
from app.platform import blob_store

#: A feed photo off a modern phone camera, uncompressed-ish. Larger than the
#: avatar cap because this is the content, not a thumbnail.
MAX_IMAGE_BYTES = 12 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}

#: Instagram allows 10; 4 keeps a carousel swipeable without a slide counter
#: and keeps one post's storage bounded.
MAX_IMAGES_PER_POST = 4


def storage_key(media_id: uuid.UUID) -> str:
    return f"social/posts/{media_id}"


def _bucket() -> str:
    """GCS bucket in prod (``GCS_BUCKET``), S3/stub bucket elsewhere."""
    s = get_settings()
    return s.gcs_bucket or s.s3_bucket


def media_url(media_id: uuid.UUID) -> str:
    """Relative URL for a post image.

    Relative (`/v1/...`) for the same reason avatars are: the web tier
    proxies ``/v1`` in every environment, so the URL works unchanged in an
    ``<img>`` tag, in the mobile WebView embeds, and — resolved against the
    API base — in a native ``<Image>``.
    """
    return f"/v1/social/posts/media/{media_id}"


async def store(media_id: uuid.UUID, body: bytes, content_type: str) -> str:
    """Write one image and return its object key. Caller commits the row."""
    key = storage_key(media_id)
    await blob_store.put_object(
        bucket=_bucket(),
        key=key,
        body=body,
        content_type=content_type,
    )
    return key


async def load(media_id: uuid.UUID) -> bytes | None:
    return await blob_store.get_object(bucket=_bucket(), key=storage_key(media_id))


def probe_size(body: bytes) -> tuple[int | None, int | None]:
    """Intrinsic (width, height) read from the image header.

    Hand-rolled rather than pulling in Pillow: we need four integers out of
    the first few dozen bytes, not decoding. The feed needs the aspect ratio
    to reserve space before the image loads — without it every post shifts
    the layout as it arrives, which is the worst jank a feed can have.

    Returns ``(None, None)`` for anything unrecognised; the clients fall
    back to a square, so an odd file degrades to a crop rather than an error.
    """
    try:
        # PNG: 8-byte signature, then IHDR with width/height as big-endian u32.
        if body[:8] == b"\x89PNG\r\n\x1a\n" and body[12:16] == b"IHDR":
            w, h = struct.unpack(">II", body[16:24])
            return int(w), int(h)

        # WebP: RIFF container, 'VP8 ' (lossy), 'VP8L' (lossless), 'VP8X'.
        if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            chunk = body[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(body[24:27], "little") + 1
                h = int.from_bytes(body[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                w = int.from_bytes(body[26:28], "little") & 0x3FFF
                h = int.from_bytes(body[28:30], "little") & 0x3FFF
                return w, h
            if chunk == b"VP8L":
                bits = int.from_bytes(body[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1

        # JPEG: walk the marker segments to the SOFn frame header.
        if body[:2] == b"\xff\xd8":
            i = 2
            end = len(body)
            while i + 9 < end:
                if body[i] != 0xFF:
                    i += 1
                    continue
                marker = body[i + 1]
                # Standalone markers carry no length field.
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = int.from_bytes(body[i + 2 : i + 4], "big")
                # SOF0-SOF15, minus the non-frame DHT/JPG/DAC markers.
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h = int.from_bytes(body[i + 5 : i + 7], "big")
                    w = int.from_bytes(body[i + 7 : i + 9], "big")
                    return w, h
                i += 2 + seg_len
    except (struct.error, IndexError, ValueError):
        return None, None
    return None, None


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_IMAGES_PER_POST",
    "MAX_IMAGE_BYTES",
    "load",
    "media_url",
    "probe_size",
    "storage_key",
    "store",
]
