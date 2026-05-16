"""Perceptual fingerprint computation.

Two-tier strategy:

* **Real path** — when an image (bytes or URL) is available we compute a
  16×16 pHash and dHash via Pillow + ImageHash.  The result is stable across
  cropping / minor color shifts so two photos of the same card collapse to
  the same (or near-equal) hex string, enabling Hamming-distance matching
  in :mod:`card_resolver_service`.

* **Fallback path** — when only image *keys* (S3 object keys) are available
  we keep the original deterministic SHA-256 derivation so existing scan
  ingestion code keeps working without an extra fetch.  Callers that need
  identity-grade hashes should prefer :func:`fingerprint_from_image_bytes`
  or :func:`fingerprint_from_image_url`.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from app.utils.logger import get_logger

logger = get_logger("services.fingerprint")


@dataclass(frozen=True)
class FingerprintResult:
    """Hashes + feature vector for duplicate detection."""

    phash: str
    dhash: str
    feature_vector: list[float]


# ----------------------------------------------------------------- real hashes


def fingerprint_from_image_bytes(data: bytes) -> FingerprintResult | None:
    """Compute a real pHash/dHash from raw JPEG/PNG bytes."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
        import imagehash  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "fingerprint: Pillow/ImageHash not installed — falling back to stub"
        )
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            phash = str(imagehash.phash(im, hash_size=16))
            dhash = str(imagehash.dhash(im, hash_size=16))
            gray = im.convert("L").resize((32, 32))
            hist = gray.histogram()
            buckets = [
                sum(hist[i * 16 : (i + 1) * 16]) / float(32 * 32)
                for i in range(16)
            ]
            vector = [round(b, 4) for b in buckets]
    except Exception as exc:
        logger.warning("fingerprint: failed to hash image (%s)", exc)
        return None
    return FingerprintResult(phash=phash, dhash=dhash, feature_vector=vector)


async def fingerprint_from_image_url(url: str) -> FingerprintResult | None:
    """Download an image and hash it. Returns ``None`` on any failure."""
    if not url:
        return None
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, follow_redirects=True)
        if resp.status_code >= 400:
            return None
        return fingerprint_from_image_bytes(resp.content)
    except (httpx.HTTPError, OSError) as exc:
        logger.info("fingerprint: download failed for %s (%s)", url, exc)
        return None


# --------------------------------------------------------------- legacy stub


def fingerprint_from_images(image_keys: dict[str, str]) -> FingerprintResult:
    """Derive deterministic placeholder hashes from S3 keys (legacy path).

    Kept for backwards compatibility with scan ingestion that only has the
    object keys at processing time. Prefer the real hashers above whenever
    image bytes are reachable.
    """
    payload = "|".join(f"{k}={v}" for k, v in sorted(image_keys.items()))
    sha = hashlib.sha256(payload.encode("utf-8")).digest()
    phash = sha[:8].hex()
    dhash = sha[8:16].hex()
    vector = [round(b / 255.0, 4) for b in sha[16:32]]
    return FingerprintResult(phash=phash, dhash=dhash, feature_vector=vector)


__all__ = [
    "FingerprintResult",
    "fingerprint_from_image_bytes",
    "fingerprint_from_image_url",
    "fingerprint_from_images",
]
