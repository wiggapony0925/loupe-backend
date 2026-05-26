"""OCR provider contract + result shapes.

The :class:`VisionProvider` Protocol is intentionally minimal: one async
method, one well-typed result. Anything beyond ``detect_text`` (cropping
hints, label detection, web entity search) belongs in a separate service
so providers stay swappable.

Coordinate system: bounding boxes are returned as ``(x, y, w, h)`` in
*pixel* coordinates of the source image, with ``(0, 0)`` at the top-left.
Confidence is in ``[0.0, 1.0]``; providers that do not expose a numeric
confidence (e.g. mocks) should return a sensible default such as ``1.0``
for synthetic data or ``0.0`` for unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class OcrError(RuntimeError):
    """Raised when an OCR call fails in a way the caller must handle.

    Pipeline code catches this and falls through to phash-only matching
    rather than propagating a 5xx to the API client. The message must
    not contain user data — it lands in structured logs.
    """


@dataclass(frozen=True, slots=True)
class OcrBlock:
    """A single contiguous text region detected on the image."""

    text: str
    confidence: float
    bbox: tuple[int, int, int, int]  # (x, y, w, h) in source-image pixels


@dataclass(frozen=True, slots=True)
class OcrResult:
    """Normalized OCR response, identical across providers.

    ``full_text`` is the provider's best concatenation of all detected
    text (usually whitespace-joined in reading order). ``blocks`` retains
    per-region detail so the identification layer can score by
    position-weighted matching (the card name usually sits in the top
    20% of the image).
    """

    full_text: str
    blocks: list[OcrBlock]
    mean_confidence: float
    language_codes: list[str] = field(default_factory=list)
    provider: str = "unknown"
    latency_ms: int = 0
    # Untyped escape hatch for provider-specific fields the ranking layer
    # may want to peek at (e.g. Vision's per-symbol breakConfidence). Not
    # part of any stable contract — never persist this verbatim.
    raw: dict[str, Any] | None = None


@runtime_checkable
class VisionProvider(Protocol):
    """Contract every OCR backend implements."""

    name: str

    async def detect_text(
        self,
        image_bytes: bytes,
        *,
        language_hints: list[str] | None = None,
    ) -> OcrResult:
        """Run text detection on ``image_bytes`` and return the unified shape.

        ``language_hints`` follow BCP-47 (e.g. ``["en"]``, ``["ja"]``).
        For Latin-alphabet card text, Google's docs recommend leaving the
        hint empty so auto-detection runs; we forward ``None`` in that
        case. Providers that don't accept hints must silently ignore them.

        Implementations MUST raise :class:`OcrError` (not the underlying
        SDK exception) on any failure — this keeps the identification
        service free of provider-specific imports in its except blocks.
        """
        ...


__all__ = ["OcrBlock", "OcrError", "OcrResult", "VisionProvider"]
