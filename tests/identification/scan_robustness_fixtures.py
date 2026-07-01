"""Lighting + angle augmentation harness for the pHash fast path.

The scanner's speed lever is the perceptual-hash fast path: if a frame's pHash
is within a few bits of a catalog art hash, we identify the card instantly with
no paid OCR. Its real-world value depends on the hash staying stable when the
same card is shot under bad light or at an angle. This module simulates those
conditions — brightness/contrast swings, glare, rotation, perspective tilt,
blur, JPEG crunch, low resolution — and measures how far the pHash drifts, using
the **production** hasher (``fingerprint_from_image_bytes``) so the numbers
reflect the shipped pipeline.

Pure Pillow + numpy, offline and deterministic. ``scripts/scan_robustness.py``
runs it over real card art; ``test_scan_robustness.py`` asserts the invariants
on generated card-like images (no network) so CI guards them.
"""

from __future__ import annotations

import io
import random
from collections.abc import Callable

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from app.services.catalog.card_fingerprint_service import (
    fingerprint_from_image_bytes,
)

# Production thresholds (see app/config.py): within FAST_PATH we skip OCR
# entirely; within MATCH we still accept the pHash as a candidate.
FAST_PATH_MAX = 6
MATCH_MAX = 12


# ────────────────────────────────────────────────────────────── pHash distance


def phash_of(img: Image.Image) -> str:
    """Production pHash hex of a PIL image (lossless PNG round-trip)."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    fp = fingerprint_from_image_bytes(buf.getvalue())
    return fp.phash if fp else ""


def hamming(a_hex: str, b_hex: str) -> int:
    """Bit Hamming distance between two equal-length hex hashes."""
    if not a_hex or not b_hex or len(a_hex) != len(b_hex):
        return 10_000
    return bin(int(a_hex, 16) ^ int(b_hex, 16)).count("1")


# ─────────────────────────────────────────────────────────────── augmentations


def _perspective(img: Image.Image, tilt: float) -> Image.Image:
    """Simulate photographing the card at a horizontal angle: one vertical
    edge is pushed in by ``tilt`` (fraction of width)."""
    w, h = img.size
    dx = int(w * tilt)
    # Map the source corners to a keystoned quad (left edge shortened).
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    dst = [(dx, int(h * tilt * 0.6)), (w, 0), (w, h), (dx, h - int(h * tilt * 0.6))]
    coeffs = _perspective_coeffs(dst, src)
    return img.transform(
        (w, h), Image.Transform.PERSPECTIVE, coeffs, resample=Image.Resampling.BICUBIC
    )


def _perspective_coeffs(src: list, dst: list) -> list[float]:
    import numpy as np

    matrix = []
    for (sx, sy), (dx, dy) in zip(src, dst, strict=True):
        matrix.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy])
        matrix.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy])
    a = np.array(matrix, dtype=float)
    b = np.array([c for pt in src for c in pt], dtype=float)
    res = np.linalg.solve(a, b)
    return list(res)


def _glare(img: Image.Image) -> Image.Image:
    """Blend a bright radial hotspot over the card (phone flash / window)."""
    w, h = img.size
    overlay = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(overlay)
    cx, cy, r = int(w * 0.62), int(h * 0.32), int(min(w, h) * 0.42)
    for i in range(r, 0, -1):
        val = int(210 * (1 - i / r))
        d.ellipse([cx - i, cy - i, cx + i, cy + i], fill=val)
    white = Image.new("RGB", (w, h), (255, 255, 255))
    return Image.composite(white, img.convert("RGB"), overlay)


def _jpeg(img: Image.Image, quality: int) -> Image.Image:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _downscale(img: Image.Image, long_edge: int) -> Image.Image:
    w, h = img.size
    s = long_edge / float(max(w, h))
    small = img.resize(
        (max(1, int(w * s)), max(1, int(h * s))), Image.Resampling.LANCZOS
    )
    return small.resize((w, h), Image.Resampling.LANCZOS)


# name → (category, transform). Categories group the report.
AUGMENTATIONS: dict[str, tuple[str, Callable[[Image.Image], Image.Image]]] = {
    "bright +70%": ("lighting", lambda im: ImageEnhance.Brightness(im).enhance(1.7)),
    "dark -55%": ("lighting", lambda im: ImageEnhance.Brightness(im).enhance(0.45)),
    "low contrast": ("lighting", lambda im: ImageEnhance.Contrast(im).enhance(0.55)),
    "high contrast": ("lighting", lambda im: ImageEnhance.Contrast(im).enhance(1.6)),
    "warm tint": ("lighting", lambda im: ImageEnhance.Color(im).enhance(1.6)),
    "glare hotspot": ("lighting", _glare),
    "rotate 3°": (
        "angle",
        lambda im: im.rotate(3, expand=False, fillcolor=(20, 20, 20)),
    ),
    "rotate 8°": (
        "angle",
        lambda im: im.rotate(8, expand=False, fillcolor=(20, 20, 20)),
    ),
    "rotate 18°": (
        "angle",
        lambda im: im.rotate(18, expand=False, fillcolor=(20, 20, 20)),
    ),
    "perspective 12%": ("angle", lambda im: _perspective(im, 0.12)),
    "perspective 22%": ("angle", lambda im: _perspective(im, 0.22)),
    "blur 1.2px": ("camera", lambda im: im.filter(ImageFilter.GaussianBlur(1.2))),
    "blur 2.5px": ("camera", lambda im: im.filter(ImageFilter.GaussianBlur(2.5))),
    "jpeg q30": ("camera", lambda im: _jpeg(im, 30)),
    "downscale 320px": ("camera", lambda im: _downscale(im, 320)),
}


def normalized(img: Image.Image) -> Image.Image:
    """Apply the same orientation/exposure normalization the pipeline does,
    so the benchmark measures the *shipped* robustness."""
    return ImageOps.autocontrast(
        (ImageOps.exif_transpose(img) or img).convert("RGB"), cutoff=1
    )


# ──────────────────────────────────────────────────────────── synthetic card


def synthetic_card(seed: int) -> Image.Image:
    """A structured, card-like image (border, title bar, art panel with shapes,
    text bars) so pHash has real spatial content — a fair stand-in for card art
    when running offline without fetching real images."""
    rng = random.Random(seed)
    w, h = 360, 504
    bg = (rng.randint(30, 90), rng.randint(30, 90), rng.randint(30, 90))
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([7, 7, w - 8, h - 8], outline=(230, 220, 120), width=6)
    d.rectangle(
        [22, 20, w - 22, 66], fill=(rng.randint(150, 240), rng.randint(120, 200), 40)
    )
    # Art panel with a few bold shapes (the "portrait").
    d.rectangle(
        [28, 78, w - 28, 300],
        fill=(rng.randint(40, 120), rng.randint(60, 140), rng.randint(80, 180)),
    )
    for _ in range(6):
        x0, y0 = rng.randint(30, w - 90), rng.randint(84, 250)
        x1, y1 = x0 + rng.randint(30, 90), y0 + rng.randint(30, 80)
        col = (rng.randint(80, 255), rng.randint(80, 255), rng.randint(80, 255))
        (d.ellipse if rng.random() < 0.5 else d.rectangle)([x0, y0, x1, y1], fill=col)
    # Text bars.
    for i in range(6):
        y = 320 + i * 26
        d.rectangle([30, y, rng.randint(160, w - 40), y + 12], fill=(210, 210, 210))
    return img


__all__ = [
    "AUGMENTATIONS",
    "FAST_PATH_MAX",
    "MATCH_MAX",
    "hamming",
    "normalized",
    "phash_of",
    "synthetic_card",
]
