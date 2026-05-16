"""Forensic grading pipeline (placeholder analyzer).

The real pipeline will analyze 4-angle TIFF/JPEG captures for centering,
surface, edges, and corners using OpenCV + ML.  This module returns a
deterministic mock grade derived from a SHA-256 of the input image keys so
callers and tests can exercise the full end-to-end flow.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SubGrades:
    """Per-axis 1–10 subgrades."""

    centering: Decimal
    corners: Decimal
    edges: Decimal
    surface: Decimal

    def as_dict(self) -> dict[str, float]:
        return {
            "centering": float(self.centering),
            "corners": float(self.corners),
            "edges": float(self.edges),
            "surface": float(self.surface),
        }


@dataclass(frozen=True)
class GradingResult:
    """Aggregate output of the grading pipeline."""

    overall: Decimal
    subgrades: SubGrades
    confidence: float


def _q(value: float) -> Decimal:
    """Quantize to 0.5 increments in [1.0, 10.0]."""
    rounded = round(value * 2) / 2
    rounded = max(1.0, min(10.0, rounded))
    return Decimal(f"{rounded:.1f}")


def grade_from_images(image_keys: dict[str, str]) -> GradingResult:
    """Produce a deterministic mock grade from a dict of angle→S3 key.

    Real implementations will load pixel data from S3.  Tests need stable
    output, so we hash the keys to derive consistent sub-grades.
    """
    if not image_keys:
        raise ValueError("grade_from_images requires at least one image key")
    payload = "|".join(f"{k}={v}" for k, v in sorted(image_keys.items()))
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    floats = [4.0 + (b / 255.0) * 6.0 for b in digest[:4]]
    centering, corners, edges, surface = floats
    subs = SubGrades(_q(centering), _q(corners), _q(edges), _q(surface))
    overall = Decimal(
        sum(map(float, [subs.centering, subs.corners, subs.edges, subs.surface]))
    ) / Decimal("4")
    overall = _q(float(overall))
    confidence = round(0.6 + (digest[4] / 255.0) * 0.4, 3)
    return GradingResult(overall=overall, subgrades=subs, confidence=confidence)


__all__ = ["GradingResult", "SubGrades", "grade_from_images"]
