"""In-memory OCR provider used by tests and the default dev config.

The real Vision API costs money and requires credentials. Tests and the
``OCR_PROVIDER=mock`` deployment use this provider instead so the full
identification pipeline can be exercised end-to-end with zero external
dependencies.

Two stocking strategies are supported:

* **Image fingerprint → canned text** (``register``). Useful for the
  eval harness, which posts a deterministic set of fixture images.
* **First-match fallback**. When no fixture is registered for the
  incoming bytes, the provider returns a generic "unknown card" payload
  so the rest of the pipeline still runs and the test asserts the
  failure path rather than crashing.
"""

from __future__ import annotations

import hashlib

from app.services.ocr.base import OcrBlock, OcrResult


class MockVisionProvider:
    """Deterministic offline OCR. Safe to use in CI and unit tests."""

    name = "mock"

    def __init__(self) -> None:
        # Key: sha256 of the image bytes (hex). Value: pre-built OcrResult.
        self._fixtures: dict[str, OcrResult] = {}
        # Optional default returned when no fixture matches. ``None`` means
        # "return an empty OcrResult" so the pipeline records a low-
        # confidence identification rather than raising.
        self._default: OcrResult | None = None

    # ---------------------------------------------------------------- setup

    def register(self, image_bytes: bytes, result: OcrResult) -> None:
        """Bind a canned :class:`OcrResult` to a specific image payload."""
        key = hashlib.sha256(image_bytes).hexdigest()
        self._fixtures[key] = result

    def register_text(self, image_bytes: bytes, text: str) -> None:
        """Convenience: register a plain string with full confidence."""
        result = OcrResult(
            full_text=text.strip(),
            blocks=[
                OcrBlock(text=line.strip(), confidence=1.0, bbox=(0, i * 20, 100, 20))
                for i, line in enumerate(text.strip().splitlines())
                if line.strip()
            ],
            mean_confidence=1.0,
            language_codes=["en"],
            provider=self.name,
            latency_ms=1,
        )
        self.register(image_bytes, result)

    def set_default(self, result: OcrResult | None) -> None:
        """Set the fallback response when no fixture matches."""
        self._default = result

    def clear(self) -> None:
        """Reset all fixtures + default (handy between tests)."""
        self._fixtures.clear()
        self._default = None

    # --------------------------------------------------------------- detect

    async def detect_text(
        self,
        image_bytes: bytes,
        *,
        language_hints: list[str] | None = None,
    ) -> OcrResult:
        key = hashlib.sha256(image_bytes).hexdigest()
        hit = self._fixtures.get(key)
        if hit is not None:
            return hit
        if self._default is not None:
            return self._default
        return OcrResult(
            full_text="",
            blocks=[],
            mean_confidence=0.0,
            language_codes=[],
            provider=self.name,
            latency_ms=1,
        )


# Module-level singleton so tests can register fixtures and the
# pipeline picks them up via :func:`get_provider`. The factory only
# instantiates one per process; using the same instance keeps the
# fixture map authoritative.
_singleton: MockVisionProvider | None = None


def get_mock_provider() -> MockVisionProvider:
    """Return the process-wide mock provider (constructing on first use)."""
    global _singleton
    if _singleton is None:
        _singleton = MockVisionProvider()
    return _singleton


__all__ = ["MockVisionProvider", "get_mock_provider"]
