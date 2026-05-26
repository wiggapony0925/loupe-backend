"""Image preprocessing used by the identification pipeline.

Two responsibilities:

* **Normalize for OCR.** Cloud Vision's docs recommend ≥1024 px on the
  long edge for OCR. We resize *down* if needed (an 8 MP phone photo
  costs network bytes without improving accuracy) and re-encode as JPEG
  at quality 85 — slightly lossier than the source, but well within the
  noise floor of phone camera JPEGs.
* **Compute identity hashes.** ``sha256`` for de-dup / cache lookup and
  the existing perceptual hash (reused via
  :mod:`app.services.catalog.card_fingerprint_service`) so the catalog
  layer's phash matching path stays available even when OCR misses.

All helpers are sync — they run inside a thread executor when called
from async code (Pillow itself releases the GIL during decode/encode).
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from app.config import get_settings
from app.services.catalog.card_fingerprint_service import (
    FingerprintResult,
    fingerprint_from_image_bytes,
)
from app.utils.logger import get_logger

logger = get_logger("services.identification.image_ops")


@dataclass(frozen=True, slots=True)
class PreparedImage:
    """The output of :func:`prepare_image_for_ocr`."""

    # Bytes to hand to the OCR provider. Always JPEG.
    ocr_bytes: bytes
    # Original SHA-256 of the *input* (not the resized copy) — used as
    # the cache key and persisted in the identification row so the same
    # upload is never billed twice.
    sha256: str
    # pHash + dHash for catalog fingerprint matching. ``None`` when
    # Pillow / ImageHash aren't available at runtime.
    fingerprint: FingerprintResult | None
    # Final (width, height) sent to OCR; logged for cost auditing.
    size: tuple[int, int]


def prepare_image_for_ocr(image_bytes: bytes) -> PreparedImage:
    """Resize + re-encode an image and compute identity hashes.

    Falls back to passing the input through unmodified if Pillow isn't
    installed — the pipeline still works, just without the bandwidth
    savings.
    """
    sha = hashlib.sha256(image_bytes).hexdigest()
    settings = get_settings()
    max_edge = max(256, int(settings.ocr_preprocess_long_edge_px))

    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - Pillow is always installed
        logger.warning("Pillow not available; sending raw bytes to OCR")
        fp = None
        try:
            fp = fingerprint_from_image_bytes(image_bytes)
        except Exception:
            fp = None
        return PreparedImage(
            ocr_bytes=image_bytes, sha256=sha, fingerprint=fp, size=(0, 0)
        )

    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            im.load()
            rgb = im.convert("RGB")
            w, h = rgb.size
            scale = min(1.0, max_edge / float(max(w, h)))
            if scale < 1.0:
                new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                rgb = rgb.resize(new_size, Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=85, optimize=True)
            ocr_bytes = buf.getvalue()
            final_size = rgb.size
    except Exception:
        logger.exception("Image preprocessing failed; falling back to raw bytes")
        ocr_bytes = image_bytes
        final_size = (0, 0)

    try:
        fp = fingerprint_from_image_bytes(image_bytes)
    except Exception:
        fp = None

    return PreparedImage(
        ocr_bytes=ocr_bytes, sha256=sha, fingerprint=fp, size=final_size
    )


__all__ = ["PreparedImage", "prepare_image_for_ocr"]
