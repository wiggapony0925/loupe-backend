"""Perceptual fingerprint computation (stub).

Real implementation will compute pHash + dHash + an embedding from the
high-res front capture.  For now we hash the image-key payload so output is
stable & testable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class FingerprintResult:
    """Hashes + feature vector for duplicate detection."""

    phash: str
    dhash: str
    feature_vector: list[float]


def fingerprint_from_images(image_keys: dict[str, str]) -> FingerprintResult:
    """Derive deterministic placeholder hashes from the keys."""
    payload = "|".join(f"{k}={v}" for k, v in sorted(image_keys.items()))
    sha = hashlib.sha256(payload.encode("utf-8")).digest()
    phash = sha[:8].hex()
    dhash = sha[8:16].hex()
    vector = [round(b / 255.0, 4) for b in sha[16:32]]
    return FingerprintResult(phash=phash, dhash=dhash, feature_vector=vector)


__all__ = ["FingerprintResult", "fingerprint_from_images"]
