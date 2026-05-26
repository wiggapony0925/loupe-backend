"""Optical Character Recognition (OCR) provider layer.

This package isolates the *text-from-pixels* concern from the rest of the
identification pipeline. The :mod:`identification` package (one floor up)
treats OCR output as just one signal among many (perceptual hash, text
match, popularity prior, user feedback). Keeping the provider interface
small means we can swap Cloud Vision out for an on-device model, an
in-house OpenCV pipeline, or a tester mock without touching the
ranking code.

Public surface:

* :class:`VisionProvider` — the Protocol every provider implements.
* :class:`OcrResult` / :class:`OcrBlock` — the unified response shape.
* :func:`get_provider` — the factory that picks a provider from settings.

Cost & safety:

* The default provider is :class:`MockVisionProvider`, which never makes
  a network call. Production must opt in by setting
  ``OCR_PROVIDER=google_vision`` AND providing
  ``GOOGLE_APPLICATION_CREDENTIALS``.
* Every provider call is wrapped with a hard timeout
  (``OCR_TIMEOUT_MS``) so a slow upstream never propagates back to a
  client request.
"""

from app.services.ocr.base import OcrBlock, OcrError, OcrResult, VisionProvider
from app.services.ocr.factory import get_provider

__all__ = [
    "OcrBlock",
    "OcrError",
    "OcrResult",
    "VisionProvider",
    "get_provider",
]
