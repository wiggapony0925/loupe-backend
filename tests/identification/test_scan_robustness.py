"""Offline guard for the scanner's pHash robustness (lighting + angles).

Runs the same augmentation harness as ``scripts/scan_robustness.py`` on generated
card-like images (no network) and asserts the invariants we ship:

  * camera degradation (blur / JPEG / low-res) stays instantly identifiable,
  * under/over-exposure normalization keeps mild lighting matchable,
  * EXIF orientation is corrected (the biggest real-world "angle" fix),
  * and it documents that pixel-level rotation/perspective is the known gap the
    OCR fallback (not the fast path) covers — so a future deskew win is visible.
"""

from __future__ import annotations

import io

import pytest

from app.services.identification.image_ops import prepare_image_for_ocr
from tests.identification.scan_robustness_fixtures import (
    AUGMENTATIONS,
    FAST_PATH_MAX,
    MATCH_MAX,
    hamming,
    normalized,
    phash_of,
    synthetic_card,
)

_SEEDS = range(8)


def _distances(names: list[str]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {n: [] for n in names}
    for seed in _SEEDS:
        img = synthetic_card(seed)
        clean = phash_of(normalized(img))
        for name in names:
            _cat, fn = AUGMENTATIONS[name]
            out[name].append(hamming(clean, phash_of(normalized(fn(img)))))
    return out


def test_camera_degradation_stays_identifiable() -> None:
    """Blur, JPEG crunch, and low-res capture must not break the match — these
    are the everyday camera conditions the fast path has to absorb."""
    d = _distances(["blur 1.2px", "jpeg q30", "downscale 320px"])
    for name, vals in d.items():
        assert max(vals) <= MATCH_MAX, f"{name} drifted too far: {vals}"
    # The cleanest of these should even clear the instant fast-path bar.
    assert max(d["downscale 320px"]) <= FAST_PATH_MAX


def test_exposure_normalization_keeps_mild_lighting_matchable() -> None:
    """Autocontrast + the hash's DCT basis make under-exposure and flat
    contrast near-lossless for identity."""
    d = _distances(["dark -55%", "low contrast"])
    for name, vals in d.items():
        assert max(vals) <= FAST_PATH_MAX, f"{name} regressed: {vals}"


def test_exif_orientation_is_corrected() -> None:
    """The shipped fix: a capture stored sideways with an EXIF orientation tag
    (every phone does this) is auto-uprighted before hashing, so it aligns to
    the catalog's upright art instead of missing by ~90°."""
    upright = synthetic_card(3).convert("RGB")

    up_buf = io.BytesIO()
    upright.save(up_buf, format="JPEG", quality=92)
    up_fp = prepare_image_for_ocr(up_buf.getvalue()).fingerprint
    assert up_fp is not None

    # Physically rotated 90° CW, tagged orientation=8 ("rotate 90° CCW to view").
    rotated = upright.rotate(-90, expand=True)
    exif = rotated.getexif()
    exif[274] = 8
    rot_buf = io.BytesIO()
    rotated.save(rot_buf, format="JPEG", quality=92, exif=exif)
    rot_fp = prepare_image_for_ocr(rot_buf.getvalue()).fingerprint
    assert rot_fp is not None

    # After orientation correction the two hashes should essentially agree.
    assert hamming(up_fp.phash, rot_fp.phash) <= MATCH_MAX


def test_rotation_is_the_documented_gap() -> None:
    """Pixel-level rotation/perspective genuinely drifts the pHash beyond match
    range (this is why OCR is the fallback, and why deskew is the next win).
    Guards the claim so nobody over-states fast-path angle robustness."""
    d = _distances(["rotate 18°", "perspective 22%"])
    for name, vals in d.items():
        assert min(vals) > MATCH_MAX, f"{name} unexpectedly matched: {vals}"


@pytest.mark.parametrize("seed", [0, 5, 11])
def test_hasher_is_stable_and_nonempty(seed: int) -> None:
    """A card hashes to the same value twice (determinism) and never empty."""
    img = synthetic_card(seed)
    a = phash_of(normalized(img))
    b = phash_of(normalized(img))
    assert a and a == b and hamming(a, b) == 0
