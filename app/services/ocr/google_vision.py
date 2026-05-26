"""Google Cloud Vision text-detection provider.

Wraps the synchronous ``google-cloud-vision`` client behind an async
adapter (the SDK doesn't ship an asyncio variant for ``annotate_image``,
so we offload the blocking call onto a worker thread).

Tuning choices (see Google's OCR best-practices doc):

* Default to ``DOCUMENT_TEXT_DETECTION`` — card faces are dense, multi-line
  layouts; ``TEXT_DETECTION`` is tuned for incidental text like signage.
  Override via ``OCR_GOOGLE_FEATURE``.
* No language hint by default. The docs explicitly recommend empty hints
  for Latin alphabets; a wrong hint can *hurt* accuracy significantly.
  Callers can still pass hints for known Japanese / Chinese cards.
* Hard ``OCR_TIMEOUT_MS`` ceiling enforced by the caller (factory wraps
  ``detect_text`` in :func:`asyncio.wait_for`).

This module is import-safe even without ``google-cloud-vision`` installed:
the SDK import is deferred until provider instantiation so test
environments and the mock-provider path stay zero-dependency.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.config import get_settings
from app.services.ocr.base import OcrBlock, OcrError, OcrResult
from app.utils.logger import get_logger

logger = get_logger("services.ocr.google_vision")


def _bbox_from_vertices(vertices: list[Any]) -> tuple[int, int, int, int]:
    """Reduce a Vision polygon (4 vertices) to an axis-aligned (x,y,w,h).

    Vision returns the bounding poly as up to 4 vertices in image-pixel
    space. We collapse to a rectangle so downstream callers don't have to
    care about the exact polygon (text on a tilted card still gives a
    sensible bounding box). Missing coordinates default to 0 because the
    Vision API omits zero-valued fields from the JSON response.
    """
    xs = [int(getattr(v, "x", 0) or 0) for v in vertices]
    ys = [int(getattr(v, "y", 0) or 0) for v in vertices]
    if not xs or not ys:
        return (0, 0, 0, 0)
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return (x0, y0, max(0, x1 - x0), max(0, y1 - y0))


class GoogleVisionProvider:
    """Async wrapper around ``google.cloud.vision.ImageAnnotatorClient``.

    The client is instantiated lazily on the first call so importing this
    module never costs a credentials check. The underlying client is
    thread-safe; we share a single instance per process.
    """

    name = "google_vision"

    def __init__(self) -> None:
        self._client: Any | None = None
        self._feature_type: str = get_settings().ocr_google_feature

    # ------------------------------------------------------------------ lazy

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            # Lazy import keeps the rest of the codebase free of a hard
            # dependency on the google-cloud-vision SDK during unit tests.
            from google.cloud import vision  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - install guarantees it
            raise OcrError("google-cloud-vision SDK is not installed") from exc
        try:
            self._client = vision.ImageAnnotatorClient()
        except Exception as exc:  # auth, network, missing creds
            # Don't leak credential paths into the error; the structured
            # log captures detail for operators.
            logger.exception("Failed to construct Vision client")
            raise OcrError("Google Vision client construction failed") from exc
        return self._client

    # --------------------------------------------------------------- detect

    async def detect_text(
        self,
        image_bytes: bytes,
        *,
        language_hints: list[str] | None = None,
    ) -> OcrResult:
        """Call Vision's annotate API and normalize to :class:`OcrResult`."""
        try:
            from google.cloud import vision  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise OcrError("google-cloud-vision SDK is not installed") from exc

        client = self._get_client()
        feature_type = (
            vision.Feature.Type.DOCUMENT_TEXT_DETECTION
            if self._feature_type == "DOCUMENT_TEXT_DETECTION"
            else vision.Feature.Type.TEXT_DETECTION
        )
        image = vision.Image(content=image_bytes)
        context = None
        if language_hints:
            context = vision.ImageContext(language_hints=list(language_hints))
        request = vision.AnnotateImageRequest(
            image=image,
            features=[vision.Feature(type_=feature_type)],
            image_context=context,
        )

        started = time.perf_counter()
        # SDK is sync; run on the default executor so the event loop
        # stays unblocked. The caller (factory) wraps this in wait_for.
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: client.batch_annotate_images(requests=[request]),
            )
        except Exception as exc:
            logger.exception("Vision annotate call failed")
            raise OcrError("Google Vision request failed") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        # batch_annotate_images returns a BatchAnnotateImagesResponse
        # containing one AnnotateImageResponse per request — we sent one.
        if not response.responses:
            raise OcrError("Google Vision returned no responses")
        annotation = response.responses[0]
        if annotation.error and annotation.error.message:
            raise OcrError(f"Vision error: {annotation.error.message}")

        return self._normalize(annotation, latency_ms=latency_ms)

    # -------------------------------------------------------------- normalize

    def _normalize(self, annotation: Any, *, latency_ms: int) -> OcrResult:
        """Convert a Vision ``AnnotateImageResponse`` to :class:`OcrResult`.

        Strategy:
        * ``full_text`` comes from ``full_text_annotation.text`` when present
          (DOCUMENT_TEXT_DETECTION), otherwise from the first entry of
          ``text_annotations[0]`` (TEXT_DETECTION's "everything" block).
        * Per-block details come from word-level entries in
          ``text_annotations[1:]`` so we have bounding boxes for the
          candidate-ranking layer.
        * Mean confidence: averaged across all word-level confidences, or
          ``0.0`` if none are present (TEXT_DETECTION sometimes omits them).
        """
        full_text = ""
        languages: list[str] = []
        confidences: list[float] = []
        blocks: list[OcrBlock] = []

        # Prefer the structured full_text_annotation when available
        # (DOCUMENT_TEXT_DETECTION populates it; TEXT_DETECTION may not).
        fta = getattr(annotation, "full_text_annotation", None)
        if fta and getattr(fta, "text", None):
            full_text = fta.text
            for page in getattr(fta, "pages", []) or []:
                for prop in getattr(page, "property", None) or []:
                    pass  # noqa - placeholder for future per-page work
                for block in getattr(page, "blocks", []) or []:
                    for para in getattr(block, "paragraphs", []) or []:
                        for word in getattr(para, "words", []) or []:
                            conf = float(getattr(word, "confidence", 0.0) or 0.0)
                            if conf > 0:
                                confidences.append(conf)
                            symbols = getattr(word, "symbols", []) or []
                            text = "".join(
                                getattr(s, "text", "") or "" for s in symbols
                            )
                            if not text:
                                continue
                            bbox = _bbox_from_vertices(
                                list(getattr(word.bounding_box, "vertices", []) or [])
                            )
                            blocks.append(OcrBlock(text=text, confidence=conf, bbox=bbox))
                            prop = getattr(word, "property", None)
                            for lang in (
                                getattr(prop, "detected_languages", []) or []
                                if prop
                                else []
                            ):
                                code = getattr(lang, "language_code", "") or ""
                                if code and code not in languages:
                                    languages.append(code)

        # Fall back to the flat ``text_annotations`` list.
        text_annotations = list(getattr(annotation, "text_annotations", []) or [])
        if not full_text and text_annotations:
            full_text = getattr(text_annotations[0], "description", "") or ""
        if not blocks:
            for ann in text_annotations[1:]:
                desc = getattr(ann, "description", "") or ""
                if not desc:
                    continue
                bbox = _bbox_from_vertices(
                    list(getattr(ann.bounding_poly, "vertices", []) or [])
                )
                conf = float(getattr(ann, "confidence", 0.0) or 0.0)
                if conf > 0:
                    confidences.append(conf)
                blocks.append(OcrBlock(text=desc, confidence=conf, bbox=bbox))

        mean_conf = (sum(confidences) / len(confidences)) if confidences else 0.0
        return OcrResult(
            full_text=full_text.strip(),
            blocks=blocks,
            mean_confidence=round(mean_conf, 4),
            language_codes=languages,
            provider=self.name,
            latency_ms=latency_ms,
            raw=None,  # don't persist proto objects
        )


__all__ = ["GoogleVisionProvider"]
