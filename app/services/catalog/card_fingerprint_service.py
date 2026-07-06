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
        import imagehash  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "fingerprint: Pillow/ImageHash not installed — falling back to stub"
        )
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            rgb = im.convert("RGB")
            phash = str(imagehash.phash(rgb, hash_size=16))
            dhash = str(imagehash.dhash(rgb, hash_size=16))
            gray = im.convert("L").resize((32, 32))
            hist = gray.histogram()
            buckets = [
                sum(hist[i * 16 : (i + 1) * 16]) / float(32 * 32) for i in range(16)
            ]
            vector = [round(b, 4) for b in buckets]
    except Exception as exc:
        logger.warning("fingerprint: failed to hash image (%s)", exc)
        return None
    return FingerprintResult(phash=phash, dhash=dhash, feature_vector=vector)


#: Query-side variants that recover real camera frames the raw hash misses.
#: Counter-rotations cancel hand tilt (a 6° tilt is ~106 bits off raw but ~28
#: after counter-rotation — measured); center-crops cancel loose framing (a
#: card-on-table frame is ~122 bits off raw but ~0 after an 82% crop). Each
#: variant is one cheap hash (~10ms); the in-memory index scan per variant is
#: milliseconds, so the whole set costs ~100ms and replaces multi-second OCR.
_VARIANT_ROTATIONS = (-6.0, -3.0, 3.0, 6.0)
_VARIANT_CROPS = (0.9, 0.82)


def fingerprint_variants_from_image_bytes(data: bytes) -> list[FingerprintResult]:
    """Fingerprints of the frame plus camera-correction variants.

    Returns the original first, then counter-rotations and center-crops.
    Empty when the image can't be decoded. Used by the identify pipeline to
    match hand-held frames against the catalog art index without OCR.
    """
    results: list[FingerprintResult] = []
    base = fingerprint_from_image_bytes(data)
    if base is None:
        return results
    results.append(base)
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            rgb = img.convert("RGB")
            variants: list[Image.Image] = []
            for deg in _VARIANT_ROTATIONS:
                variants.append(rgb.rotate(deg, expand=False, fillcolor=(24, 24, 28)))
            w, h = rgb.size
            for frac in _VARIANT_CROPS:
                cw, ch = int(w * frac), int(h * frac)
                box = ((w - cw) // 2, (h - ch) // 2, (w + cw) // 2, (h + ch) // 2)
                variants.append(rgb.crop(box).resize((w, h)))
            import imagehash  # type: ignore[import-not-found]

            for v in variants:
                results.append(
                    FingerprintResult(
                        phash=str(imagehash.phash(v, hash_size=16)),
                        dhash=str(imagehash.dhash(v, hash_size=16)),
                        feature_vector=base.feature_vector,
                    )
                )
    except Exception as exc:  # pragma: no cover — variant failure is non-fatal
        logger.info("fingerprint variants failed (%s); using base only", exc)
    return results


# Some image CDNs (notably Scryfall's cards.scryfall.io) reject the default
# ``python-httpx/x.y`` User-Agent with a 400 — their documented bot policy. A
# real UA is required, so send one for every fingerprint download.
_IMAGE_FETCH_HEADERS = {
    "User-Agent": "LoupeScanner/1.0 (+https://loupe.app)",
    "Accept": "image/*,*/*",
}


async def fingerprint_from_image_url(url: str) -> FingerprintResult | None:
    """Download an image and hash it. Returns ``None`` on any failure."""
    if not url:
        return None
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url, follow_redirects=True, headers=_IMAGE_FETCH_HEADERS
            )
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
