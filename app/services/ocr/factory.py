"""Factory that returns the configured :class:`VisionProvider` singleton.

The factory is the only module that knows about every provider, so
:mod:`identification` and the router stay decoupled from individual
backends.

A wrapper provider (``_TimedProvider``) enforces the shared
``OCR_TIMEOUT_MS`` ceiling so any new provider automatically inherits
the latency budget — providers don't have to remember to implement it
themselves.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.services.ocr.base import OcrError, OcrResult, VisionProvider
from app.services.ocr.mock import get_mock_provider
from app.utils.logger import get_logger

logger = get_logger("services.ocr.factory")


class _TimedProvider:
    """Decorator that enforces the global OCR timeout on any provider."""

    def __init__(self, inner: VisionProvider, *, timeout_ms: int) -> None:
        self._inner = inner
        self._timeout_s = max(0.1, timeout_ms / 1000.0)
        self.name = inner.name

    async def detect_text(
        self,
        image_bytes: bytes,
        *,
        language_hints: list[str] | None = None,
    ) -> OcrResult:
        try:
            return await asyncio.wait_for(
                self._inner.detect_text(image_bytes, language_hints=language_hints),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError as exc:
            logger.warning(
                "OCR provider %s timed out after %.1fs",
                self._inner.name,
                self._timeout_s,
            )
            raise OcrError(
                f"OCR provider '{self._inner.name}' exceeded {self._timeout_s:.1f}s"
            ) from exc


# Cached per-process instance keyed by (provider_name, timeout_ms). We
# avoid lru_cache because the inner client should be reused even across
# tests where settings are reloaded.
_cache: dict[tuple[str, int], _TimedProvider] = {}


def get_provider(name: str | None = None) -> VisionProvider:
    """Return the configured :class:`VisionProvider`.

    Resolution order:

    1. Explicit ``name`` argument (used by the eval harness to A/B test).
    2. ``OCR_PROVIDER`` env / settings value.
    3. ``mock`` (last-resort default — never costs money).
    """
    settings = get_settings()
    chosen = (name or settings.ocr_provider or "mock").lower()
    cache_key = (chosen, settings.ocr_timeout_ms)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    inner: VisionProvider
    if chosen == "google_vision":
        # Local import so unit tests never touch the SDK.
        from app.services.ocr.google_vision import GoogleVisionProvider

        inner = GoogleVisionProvider()
    elif chosen == "mock":
        inner = get_mock_provider()
    else:
        logger.warning("Unknown OCR_PROVIDER=%r — falling back to mock", chosen)
        inner = get_mock_provider()

    wrapped = _TimedProvider(inner, timeout_ms=settings.ocr_timeout_ms)
    _cache[cache_key] = wrapped
    return wrapped


def reset_provider_cache() -> None:
    """Clear cached providers (tests that mutate settings call this)."""
    _cache.clear()


# Expose the underlying types so callers can ``isinstance``-check during
# tests without importing the provider modules directly.
__all__: list[str] = ["get_provider", "reset_provider_cache"]
