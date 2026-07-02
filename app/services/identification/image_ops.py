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

import base64
import hashlib
import io
from dataclasses import dataclass
from typing import Any

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
    # Small orientation-normalized JPEG thumbnail (base64, no data-url
    # prefix) of the *actual photo the user scanned* — persisted so the
    # admin scan-history log can show what was in front of the camera.
    # ``None`` when Pillow is unavailable or thumbnailing failed.
    thumb_b64: str | None = None


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
        from PIL import Image, ImageOps  # type: ignore[import-not-found]
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

    ocr_bytes = image_bytes
    final_size = (0, 0)
    fp = None
    thumb_b64: str | None = None
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            im.load()
            # 1) Respect camera orientation FIRST. Phone photos carry an EXIF
            #    rotation tag; if we ignore it a "sideways" capture is fed to
            #    OCR + pHash rotated 90°, so neither the text nor the art hash
            #    matches. exif_transpose bakes the rotation into the pixels so
            #    the frame is upright — the single biggest real-world angle fix.
            base = ImageOps.exif_transpose(im) or im
            base = base.convert("RGB")

        # 2) Fingerprint from the *orientation-normalized* image (lossless PNG),
        #    so a rotated capture aligns to the catalog's upright art hash.
        norm_buf = io.BytesIO()
        base.save(norm_buf, format="PNG")
        try:
            fp = fingerprint_from_image_bytes(norm_buf.getvalue())
        except Exception:
            fp = None

        # 3) Review thumbnail: a small, true-to-life JPEG of the *actual*
        #    frame (from the oriented `base`, NOT the autocontrasted OCR copy)
        #    for the admin scan-history log. Best-effort — never fail the scan
        #    over a thumbnail.
        try:
            thumb_b64 = _encode_thumbnail(base, Image, ImageOps)
        except Exception:
            thumb_b64 = None

        # 4) OCR copy: normalize exposure so under/over-lit photos read. Vision
        #    OCR is much more reliable on an auto-contrasted frame; a small
        #    cutoff clips sensor noise + glare without crushing the midtones.
        ocr_img = ImageOps.autocontrast(base, cutoff=1)
        w, h = ocr_img.size
        scale = min(1.0, max_edge / float(max(w, h)))
        if scale < 1.0:
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            ocr_img = ocr_img.resize(new_size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        ocr_img.save(buf, format="JPEG", quality=85, optimize=True)
        ocr_bytes = buf.getvalue()
        final_size = ocr_img.size
    except Exception:
        logger.exception("Image preprocessing failed; falling back to raw bytes")
        ocr_bytes = image_bytes
        final_size = (0, 0)
        if fp is None:
            try:
                fp = fingerprint_from_image_bytes(image_bytes)
            except Exception:
                fp = None

    return PreparedImage(
        ocr_bytes=ocr_bytes,
        sha256=sha,
        fingerprint=fp,
        size=final_size,
        thumb_b64=thumb_b64,
    )


# Long edge of the stored review thumbnail. Small enough that a base64 copy
# TOASTs cheaply in Postgres yet stays legible in the admin history grid.
_THUMB_MAX_EDGE = 320


def _encode_thumbnail(base: Any, image_mod: Any, image_ops: Any) -> str | None:
    """Return a base64 JPEG thumbnail (no data-url prefix) of ``base``."""
    thumb = image_ops.contain(
        base, (_THUMB_MAX_EDGE, _THUMB_MAX_EDGE), image_mod.Resampling.LANCZOS
    )
    out = io.BytesIO()
    thumb.save(out, format="JPEG", quality=60, optimize=True)
    return base64.b64encode(out.getvalue()).decode("ascii")


__all__ = ["PreparedImage", "prepare_image_for_ocr"]
