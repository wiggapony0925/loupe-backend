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
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}

#: Video, capped an order of magnitude higher because thirty seconds off a
#: phone is tens of megabytes and there is no server-side transcode to lean
#: on. The clients record at a bounded resolution and duration; this is the
#: backstop for anything that arrives from elsewhere.
MAX_VIDEO_BYTES = 120 * 1024 * 1024
#: MP4/H.264 and QuickTime — what iOS and Android actually produce. No WebM:
#: iOS cannot play it, and accepting an upload that half the users can't
#: watch is worse than refusing it.
VIDEO_CONTENT_TYPES = {"video/mp4", "video/quicktime"}

ALLOWED_CONTENT_TYPES = IMAGE_CONTENT_TYPES | VIDEO_CONTENT_TYPES

#: Instagram allows 10; 4 keeps a carousel swipeable without a slide counter
#: and keeps one post's storage bounded.
MAX_IMAGES_PER_POST = 4


def is_video(content_type: str) -> bool:
    return content_type in VIDEO_CONTENT_TYPES


def max_bytes(content_type: str) -> int:
    """The size ceiling for one upload of this kind."""
    return MAX_VIDEO_BYTES if is_video(content_type) else MAX_IMAGE_BYTES


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
    """Intrinsic (width, height) read from the file header.

    Handles stills and MP4/QuickTime video — see :func:`_probe_mp4` for the
    latter.

    Hand-rolled rather than pulling in Pillow: we need four integers out of
    the first few dozen bytes, not decoding. The feed needs the aspect ratio
    to reserve space before the image loads — without it every post shifts
    the layout as it arrives, which is the worst jank a feed can have.

    Returns ``(None, None)`` for anything unrecognised; the clients fall
    back to a square, so an odd file degrades to a crop rather than an error.
    """
    try:
        # MP4 / QuickTime: 'ftyp' is the first box in both.
        if body[4:8] == b"ftyp":
            return _probe_mp4(body)

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


def _probe_mp4(body: bytes) -> tuple[int | None, int | None]:
    """Display size of an MP4/QuickTime video, from its first ``tkhd`` box.

    An ISO-BMFF file is a tree of length-prefixed boxes. The track header
    (``tkhd``) carries the display width and height as 16.16 fixed-point in
    its last eight bytes — the only two numbers a client needs to reserve
    the right frame before the video streams.

    Scanned linearly rather than walked properly. A correct walk means
    parsing moov ▸ trak ▸ tkhd with sizes and versions at every level, to
    reach a value we tolerate not finding: unknown dimensions fall back to
    a square, exactly as they do for a malformed JPEG. The scan is bounded
    to the first 512 KB so a large upload can't turn this into a linear
    read of a hundred megabytes.

    Takes the FIRST tkhd with a non-zero size, which is the video track —
    an audio track's tkhd carries zeroes, so it self-skips.
    """
    window = body[: 512 * 1024]
    start = 0
    while True:
        at = window.find(b"tkhd", start)
        if at == -1:
            return None, None
        # Box layout: [4B size][4B type][1B version][3B flags][payload…].
        # Everything after the version differs in width by 4 bytes per
        # 64-bit field, so index backwards from the end of the box instead.
        version = window[at + 4] if at + 4 < len(window) else 0
        # v0 payload is 84 bytes after the type; v1 adds 12 for 64-bit times.
        payload = 84 if version == 0 else 96
        end = at + 4 + payload
        if end <= len(window):
            w = int.from_bytes(window[end - 8 : end - 4], "big") >> 16
            h = int.from_bytes(window[end - 4 : end], "big") >> 16
            if w > 0 and h > 0:
                return w, h
        start = at + 4


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "IMAGE_CONTENT_TYPES",
    "MAX_IMAGES_PER_POST",
    "MAX_IMAGE_BYTES",
    "MAX_VIDEO_BYTES",
    "VIDEO_CONTENT_TYPES",
    "is_video",
    "load",
    "max_bytes",
    "media_url",
    "probe_size",
    "storage_key",
    "store",
]
