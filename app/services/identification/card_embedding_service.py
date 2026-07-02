"""Image-embedding + nearest-neighbour scaffold for card identification.

This is the interface layer for the "embeddings + pgvector" identification
upgrade: ``embed_image_bytes`` → a fixed-length L2-normalised vector, ``cosine``
→ similarity, so a resolver can rank the catalog by nearest-neighbour (linear
scan now; a pgvector ``<=>`` index when the catalog outgrows it — the path the
pHash resolver already notes). Swapping the feature extractor keeps this
interface unchanged.

The extractor here is CLASSICAL and dependency-light — an HSV colour histogram
(lighting-robust) plus a radial colour profile (average colour in concentric
rings). Both are **rotation-invariant by construction**, so it closes the exact
gap pHash has (angles: see ``scripts/scan_robustness.py``): a card shot at 25°
stays ~0.90 cosine to its upright self, where the pHash drifts >120 bits.

**But it is NOT wired into the live resolver, on purpose.** Measured on real
cards it is rotation-robust yet **not discriminative enough**: different cards
that share a palette (Charizard vs Pikachu ≈ 0.96) can score *higher* than the
same card rotated (≈ 0.90). Colour statistics alone can't tell Pokémon cards
apart — that's precisely why production scanners use a **learned CNN encoder**.
So this module is the plumbing + benchmark, ready for the feature extractor to
be replaced with a CNN (ONNX Runtime) — the drop-in that makes it production
grade. See ``scripts/embedding_eval.py`` for the robustness-vs-discrimination
numbers, and ``tests/identification/test_embedding.py`` for the invariants.
"""

from __future__ import annotations

import io

from app.utils.logger import get_logger

logger = get_logger("services.identification.embedding")

# Canonical working size + feature layout. EMBED_DIM must stay in sync with the
# concatenation below (h+s+v hist + radial rings x 3 channels).
_CANON = 160
_H_BINS = 18
_S_BINS = 6
_V_BINS = 6
_RINGS = 8
EMBED_DIM = _H_BINS + _S_BINS + _V_BINS + _RINGS * 3  # 30 + 24 = 54


def embed_image_bytes(data: bytes) -> list[float] | None:
    """Compute the rotation/lighting-robust embedding for a card image.

    Returns an ``EMBED_DIM``-length L2-normalised vector, or ``None`` if the
    imaging stack is unavailable or the bytes don't decode.
    """
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow/numpy are in requirements
        logger.warning("embedding: numpy/Pillow unavailable")
        return None

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            rgb = ImageOps.exif_transpose(im) or im
            rgb = rgb.convert("RGB").resize((_CANON, _CANON), Image.Resampling.BILINEAR)
        arr = np.asarray(rgb, dtype=np.float32) / 255.0  # (H, W, 3)
    except Exception as exc:
        logger.info("embedding: decode failed (%s)", exc)
        return None

    # ── HSV colour histogram — rotation-invariant + exposure-robust. ──
    hsv = _rgb_to_hsv(arr, np)
    h_hist = _hist(hsv[..., 0], _H_BINS, np)
    s_hist = _hist(hsv[..., 1], _S_BINS, np)
    v_hist = _hist(hsv[..., 2], _V_BINS, np)

    # ── Radial colour profile — average RGB in concentric rings about the
    #    centre. Rotating the card doesn't move colour between rings. ──
    radial = _radial_profile(arr, _RINGS, np)  # (RINGS, 3)

    vec = np.concatenate([h_hist, s_hist, v_hist, radial.reshape(-1)]).astype(
        np.float32
    )
    norm = float(np.linalg.norm(vec))
    if norm < 1e-8:
        return None
    return (vec / norm).tolist()


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two embeddings (both assumed L2-normalised)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return max(-1.0, min(1.0, dot))


# ─────────────────────────────────────────────────────────────── internals


def _rgb_to_hsv(arr, np):  # arr: (H,W,3) in [0,1]
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    diff = mx - mn
    # Hue in [0,1)
    hue = np.zeros_like(mx)
    mask = diff > 1e-6
    rc = np.where(mask, (mx - r) / np.where(mask, diff, 1), 0)
    gc = np.where(mask, (mx - g) / np.where(mask, diff, 1), 0)
    bc = np.where(mask, (mx - b) / np.where(mask, diff, 1), 0)
    hue = np.where((mx == r) & mask, (bc - gc), hue)
    hue = np.where((mx == g) & mask, 2.0 + (rc - bc), hue)
    hue = np.where((mx == b) & mask, 4.0 + (gc - rc), hue)
    hue = (hue / 6.0) % 1.0
    sat = np.where(mx > 1e-6, diff / np.where(mx > 1e-6, mx, 1), 0)
    val = mx
    return np.stack([hue, sat, val], axis=-1)


def _hist(channel, bins, np):
    h, _ = np.histogram(channel, bins=bins, range=(0.0, 1.0))
    total = float(h.sum())
    return (h / total) if total > 0 else h.astype(float)


def _radial_profile(arr, rings, np):
    h, w, _ = arr.shape
    yy, xx = np.mgrid[0:h, 0:w]
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    dist = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    dist = dist / (dist.max() + 1e-6)  # normalise to [0,1]
    out = np.zeros((rings, 3), dtype=np.float32)
    edges = np.linspace(0.0, 1.0, rings + 1)
    for i in range(rings):
        m = (dist >= edges[i]) & (dist < edges[i + 1] + (1e-6 if i == rings - 1 else 0))
        if m.any():
            out[i] = arr[m].mean(axis=0)
    return out


__all__ = ["EMBED_DIM", "cosine", "embed_image_bytes"]
