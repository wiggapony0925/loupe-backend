"""End-to-end image → ranked candidates pipeline.

:class:`CardIdentifier` is the single entry point used by both the HTTP
router (``POST /v1/cards/identify``) and the eval harness. The class
itself is stateless apart from the OCR provider; every call accepts the
DB session and request-scoped context it needs, which keeps testing
trivial (no monkeypatching globals).

Algorithm summary:

    1. Preprocess image (resize, JPEG re-encode, sha256, phash).
    2. OCR via the configured :class:`VisionProvider` (mock by default).
    3. Parse OCR text into structured fields (title, set code, …).
    4. Infer TCG (user hint → parsed hints → fan-out to all).
    5. Catalog text search: for each title candidate, query
       :mod:`card_search_service` with the inferred TCG.
    6. Phash fallback: try :func:`card_resolver_service.resolve_by_phash`
       so previously-graded user cards can be recognised even when text
       OCR misses.
    7. De-duplicate + score each candidate via
       :func:`confidence.score_candidate`.
    8. Apply feedback priors (recent correct confirmations on similar
       OCR text gently lift those candidates).
    9. Persist a :class:`CardIdentification` row capturing every signal so
       the analytics endpoint can compute accuracy + cost.
   10. Return the top ``OCR_MAX_CANDIDATES`` plus the ``identification_id``
       the client uses to attach feedback.

All steps degrade gracefully — an OCR failure, an upstream timeout, or
an empty candidate set yields an :class:`IdentifyOutcome` with an empty
``candidates`` list and ``accuracy_score=0.0`` rather than a 5xx.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.catalog import card_resolver_service, card_search_service
from app.services.identification.confidence import (
    ScoreBreakdown,
    infer_tcg,
    score_candidate,
)
from app.services.identification.image_ops import (
    PreparedImage,
    prepare_image_for_ocr,
)
from app.services.identification.text_parser import ParsedCard, parse_ocr_text
from app.services.ocr import OcrError, OcrResult, VisionProvider, get_provider
from app.services.ocr.budget import is_budget_exceeded, record_spend_increment
from app.utils.logger import get_logger

logger = get_logger("services.identification")


@dataclass(frozen=True, slots=True)
class CandidateOut:
    """Serialisable candidate suitable for the API response."""

    card_id: str | None  # local UUID when known
    upstream_id: str | None  # ``<source>:<external_id>``
    name: str
    set_name: str | None
    set_code: str | None
    number: str | None
    image_url: str | None
    tcg: str | None
    confidence: float
    source: str  # "text" | "phash" | "feedback"
    breakdown: dict[str, float]


@dataclass(frozen=True, slots=True)
class IdentifyOutcome:
    """What :meth:`CardIdentifier.identify` returns."""

    identification_id: uuid.UUID
    candidates: list[CandidateOut]
    accuracy_score: float  # top-1 confidence, 0..1
    primary_source: str  # which signal won ("text" | "phash" | "none")
    parsed: ParsedCard
    ocr: OcrResult
    latency_ms: int
    tcg_inferred: str
    cost_usd: float
    # When ``True`` the server refused to call the paid OCR provider
    # (typically because ``OCR_MONTHLY_BUDGET_USD`` is exhausted) and
    # the client should run on-device OCR + resubmit via
    # ``POST /v1/cards/identify/text``. ``candidates`` will be empty.
    fallback_required: bool = False
    fallback_reason: str | None = None


class CardIdentifier:
    """Orchestrates OCR + catalog search + ranking + persistence."""

    def __init__(self, provider: VisionProvider | None = None) -> None:
        # Provider injection keeps unit tests free of singleton state. In
        # production the factory hands back the configured + timed wrapper.
        self._provider = provider or get_provider()

    # ─────────────────────────────────────────────────────────── public API

    async def identify(
        self,
        db: AsyncSession,
        *,
        image_bytes: bytes,
        tcg_hint: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> IdentifyOutcome:
        """Run the full pipeline and persist a :class:`CardIdentification`."""
        started = time.perf_counter()
        settings = get_settings()

        # Step 1 — preprocess (runs on a thread to avoid blocking the loop).
        prepared: PreparedImage = await asyncio.to_thread(
            prepare_image_for_ocr, image_bytes
        )

        # Step 2 — OCR. Errors become an empty result so the pipeline still
        # produces a (low-confidence) identification + analytics row.
        #
        # Budget guard: if the configured provider costs money and the
        # monthly cap is hit, skip the call entirely. The client gets a
        # ``fallback_required=True`` response and is expected to retry
        # via the cheaper ``/identify/text`` route after running
        # on-device OCR (Apple Vision / ML Kit).
        provider_costs_money = self._provider.name == "google_vision"
        budget_blocked = False
        if provider_costs_money:
            try:
                budget_blocked = await is_budget_exceeded(db)
            except Exception:
                # Never let a budget bookkeeping failure take down OCR.
                logger.exception("budget check failed; assuming not exceeded")
                budget_blocked = False

        ocr_result: OcrResult
        if budget_blocked:
            logger.warning(
                "OCR budget exceeded ($%.2f cap); signalling client fallback",
                settings.ocr_monthly_budget_usd,
            )
            ocr_result = OcrResult(
                full_text="",
                blocks=[],
                mean_confidence=0.0,
                language_codes=[],
                provider="client_fallback",
                latency_ms=0,
            )
        else:
            try:
                ocr_result = await self._provider.detect_text(prepared.ocr_bytes)
            except OcrError as exc:
                logger.warning("OCR failed (%s): %s", self._provider.name, exc)
                ocr_result = OcrResult(
                    full_text="",
                    blocks=[],
                    mean_confidence=0.0,
                    language_codes=[],
                    provider=self._provider.name,
                    latency_ms=0,
                )

        # Step 3 — parse.
        parsed = parse_ocr_text(ocr_result.full_text)

        # Step 4 — infer TCG.
        tcg_inferred = infer_tcg(parsed, user_hint=tcg_hint)

        # Steps 5+6 — gather candidates from text search + phash in parallel.
        text_task = self._search_text(parsed=parsed, tcg=tcg_inferred)
        phash_task = self._search_phash(
            db, phash=prepared.fingerprint.phash if prepared.fingerprint else None
        )
        text_candidates, phash_candidate = await asyncio.gather(
            text_task, phash_task, return_exceptions=False
        )

        # Step 7 — de-duplicate by upstream_id (preferred) or name+set.
        merged = self._dedupe(text_candidates, phash_candidate)

        # Step 8 — feedback prior. Cheap: only query when we have a title.
        feedback_priors: dict[str, float] = {}
        if parsed.title:
            try:
                feedback_priors = await self._feedback_priors(
                    db, ocr_text=ocr_result.full_text, parsed_title=parsed.title
                )
            except Exception:
                # Feedback table may not exist yet on a fresh DB; the
                # pipeline must keep working without it.
                logger.exception("feedback prior lookup failed")
                feedback_priors = {}

        # Step 9 — score every candidate.
        scored: list[tuple[CandidateOut, ScoreBreakdown]] = []
        for cand, source in merged:
            cand_id = cand.get("id") or ""
            prior = feedback_priors.get(cand_id, 0.0)
            breakdown = score_candidate(
                parsed=parsed,
                candidate=cand,
                ocr_confidence=ocr_result.mean_confidence,
                phash_hit=(source == "phash"),
                feedback_prior=prior,
            )
            scored.append((self._to_candidate(cand, source, breakdown), breakdown))
        scored.sort(key=lambda pair: pair[0].confidence, reverse=True)
        top_candidates = [pair[0] for pair in scored[: settings.ocr_max_candidates]]

        # Step 10 — persist.
        accuracy = top_candidates[0].confidence if top_candidates else 0.0
        primary_source = top_candidates[0].source if top_candidates else "none"
        latency_ms = int((time.perf_counter() - started) * 1000)
        # Only bill for calls that actually hit Vision. Budget-blocked
        # short-circuits and provider errors must not inflate spend.
        billed = (
            provider_costs_money
            and not budget_blocked
            and ocr_result.provider == "google_vision"
        )
        cost_usd = settings.ocr_google_cost_usd_per_call if billed else 0.0
        if billed:
            record_spend_increment(cost_usd)
        identification_id = await self._persist(
            db,
            user_id=user_id,
            prepared=prepared,
            ocr_result=ocr_result,
            parsed=parsed,
            tcg_inferred=tcg_inferred,
            candidates=top_candidates,
            accuracy=accuracy,
            primary_source=primary_source,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
        )

        return IdentifyOutcome(
            identification_id=identification_id,
            candidates=top_candidates,
            accuracy_score=accuracy,
            primary_source=primary_source,
            parsed=parsed,
            ocr=ocr_result,
            latency_ms=latency_ms,
            tcg_inferred=tcg_inferred,
            cost_usd=cost_usd,
            fallback_required=budget_blocked,
            fallback_reason=(
                f"monthly OCR budget ${settings.ocr_monthly_budget_usd:.2f} exhausted"
                if budget_blocked
                else None
            ),
        )

    async def identify_from_text(
        self,
        db: AsyncSession,
        *,
        ocr_text: str,
        tcg_hint: str | None = None,
        client_provider: str = "client_fallback",
        ocr_confidence: float = 0.0,
        user_id: uuid.UUID | None = None,
    ) -> IdentifyOutcome:
        """Run the pipeline against text the *client* already OCR'd.

        Used when the server returns ``fallback_required=True`` because
        the Vision budget is exhausted. The client runs on-device OCR
        (Apple Vision / ML Kit) and resubmits the parsed text here. We
        skip steps 1 + 2 (preprocess + paid OCR) and run parse →
        catalog search → score → persist.
        """
        import hashlib

        started = time.perf_counter()
        settings = get_settings()

        ocr_result = OcrResult(
            full_text=ocr_text or "",
            blocks=[],
            mean_confidence=max(0.0, min(1.0, ocr_confidence)),
            language_codes=[],
            provider=client_provider,
            latency_ms=0,
        )
        # Synthetic sha256 keyed on the parsed text so repeated submits
        # of the same OCR string land on the same identification row.
        synthetic_sha = hashlib.sha256(
            ("text::" + (ocr_text or "")).encode("utf-8")
        ).hexdigest()

        parsed = parse_ocr_text(ocr_result.full_text)
        tcg_inferred = infer_tcg(parsed, user_hint=tcg_hint)
        text_candidates = await self._search_text(parsed=parsed, tcg=tcg_inferred)
        merged = self._dedupe(text_candidates, None)

        feedback_priors: dict[str, float] = {}
        if parsed.title:
            try:
                feedback_priors = await self._feedback_priors(
                    db, ocr_text=ocr_result.full_text, parsed_title=parsed.title
                )
            except Exception:
                logger.exception("feedback prior lookup failed (text path)")

        scored: list[tuple[CandidateOut, ScoreBreakdown]] = []
        for cand, source in merged:
            cand_id = cand.get("id") or ""
            prior = feedback_priors.get(cand_id, 0.0)
            breakdown = score_candidate(
                parsed=parsed,
                candidate=cand,
                ocr_confidence=ocr_result.mean_confidence,
                phash_hit=False,
                feedback_prior=prior,
            )
            scored.append((self._to_candidate(cand, source, breakdown), breakdown))
        scored.sort(key=lambda pair: pair[0].confidence, reverse=True)
        top_candidates = [pair[0] for pair in scored[: settings.ocr_max_candidates]]

        accuracy = top_candidates[0].confidence if top_candidates else 0.0
        primary_source = top_candidates[0].source if top_candidates else "none"
        latency_ms = int((time.perf_counter() - started) * 1000)

        # Persist via a lightweight stub so we share schema with the
        # image path. ``image_sha256`` carries our synthetic key.
        from app.models.identification import CardIdentification

        top = top_candidates[0] if top_candidates else None
        row = CardIdentification(
            user_id=user_id,
            image_sha256=synthetic_sha,
            phash=None,
            ocr_provider=ocr_result.provider,
            ocr_full_text=(ocr_result.full_text or "")[:8000],
            ocr_confidence=ocr_result.mean_confidence,
            parsed_title=(parsed.title or "")[:200] or None,
            parsed_set_code=parsed.set_code,
            parsed_card_number=parsed.card_number,
            tcg_inferred=tcg_inferred,
            primary_source=primary_source,
            top_card_id=top.card_id if top else None,
            top_upstream_id=top.upstream_id if top else None,
            top_confidence=accuracy,
            candidates_json=[
                {
                    "card_id": c.card_id,
                    "upstream_id": c.upstream_id,
                    "name": c.name,
                    "confidence": c.confidence,
                    "source": c.source,
                    "breakdown": c.breakdown,
                }
                for c in top_candidates
            ],
            latency_ms=latency_ms,
            cost_usd=0.0,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

        return IdentifyOutcome(
            identification_id=row.id,
            candidates=top_candidates,
            accuracy_score=accuracy,
            primary_source=primary_source,
            parsed=parsed,
            ocr=ocr_result,
            latency_ms=latency_ms,
            tcg_inferred=tcg_inferred,
            cost_usd=0.0,
            fallback_required=False,
            fallback_reason=None,
        )

    async def record_feedback(
        self,
        db: AsyncSession,
        *,
        identification_id: uuid.UUID,
        correct: bool,
        chosen_card_id: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> None:
        """Persist a user's thumbs-up/down + optional correction.

        ``chosen_card_id`` may be either a local Card UUID or a composite
        upstream_id (``"pokemontcg:base1-4"``) so the client can answer
        even for not-yet-materialized catalog rows.
        """
        # Local import keeps the module importable before migrations run.
        from sqlalchemy import select

        from app.models.identification import (
            CardIdentification,
            IdentificationFeedback,
        )

        existing = (
            await db.execute(
                select(CardIdentification).where(
                    CardIdentification.id == identification_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise ValueError("identification_id not found")

        db.add(
            IdentificationFeedback(
                identification_id=identification_id,
                user_id=user_id,
                correct=correct,
                chosen_card_id=(chosen_card_id or None),
            )
        )
        await db.commit()

    # ────────────────────────────────────────────────────────────── helpers

    async def _search_text(
        self, *, parsed: ParsedCard, tcg: str
    ) -> list[dict[str, Any]]:
        """Run catalog text search across each title candidate."""
        if not parsed.title_candidates and not parsed.title:
            return []
        # Try the top title; only fall through to runner-ups if it yields
        # nothing. Avoids tripling upstream cost on every call.
        seen: dict[str, dict[str, Any]] = {}
        titles = [t for t, _ in parsed.title_candidates] or [parsed.title or ""]
        for title in titles:
            if not title:
                continue
            try:
                body = await card_search_service.search_cards(
                    q=title, tcg=tcg, limit=10
                )
            except Exception:
                logger.exception("text search failed for title=%r tcg=%s", title, tcg)
                continue
            for result in body.get("results", []) or []:
                key = (
                    result.get("id")
                    or f"{result.get('name')}::{(result.get('set') or {}).get('id')}"
                )
                if key and key not in seen:
                    seen[key] = result
            if seen:
                break  # don't blow upstream budget on backups when we found hits
        return list(seen.values())

    async def _search_phash(
        self, db: AsyncSession, *, phash: str | None
    ) -> dict[str, Any] | None:
        """Try the existing phash resolver; ``None`` when no match."""
        if not phash:
            return None
        try:
            resolved = await card_resolver_service.resolve_by_phash(db, phash)
        except Exception:
            logger.exception("phash resolve failed")
            return None
        if resolved is None or resolved.unified is None:
            return None
        return resolved.unified

    @staticmethod
    def _dedupe(
        text_results: list[dict[str, Any]],
        phash_result: dict[str, Any] | None,
    ) -> list[tuple[dict[str, Any], str]]:
        """Merge text + phash candidates, preferring the phash-tagged one."""
        keyed: dict[str, tuple[dict[str, Any], str]] = {}
        for cand in text_results:
            key = cand.get("id") or cand.get("name") or ""
            if not key:
                continue
            keyed[key] = (cand, "text")
        if phash_result is not None:
            key = phash_result.get("id") or phash_result.get("name") or ""
            if key:
                # phash always wins over text for the same card — it's a
                # stronger identity signal.
                keyed[key] = (phash_result, "phash")
        return list(keyed.values())

    async def _feedback_priors(
        self,
        db: AsyncSession,
        *,
        ocr_text: str,
        parsed_title: str,
    ) -> dict[str, float]:
        """Look up recent ``correct=True`` feedback for similar OCR text.

        Returns a map of ``card_id_or_upstream_id`` → ``[0,1]`` boost.
        """
        from datetime import datetime, timedelta

        from sqlalchemy import func, select

        from app.models.identification import (
            CardIdentification,
            IdentificationFeedback,
        )

        settings = get_settings()
        cutoff = datetime.now(UTC) - timedelta(
            days=settings.ocr_feedback_boost_window_days
        )

        # Pull recent positive feedback joined to their identifications.
        # We deliberately keep the comparison cheap — exact (case-insensitive)
        # title match is good enough until we have enough volume to justify
        # a trigram index.
        stmt = (
            select(
                IdentificationFeedback.chosen_card_id,
                func.count(IdentificationFeedback.id).label("hits"),
            )
            .join(
                CardIdentification,
                CardIdentification.id == IdentificationFeedback.identification_id,
            )
            .where(IdentificationFeedback.correct.is_(True))
            .where(IdentificationFeedback.chosen_card_id.is_not(None))
            .where(CardIdentification.created_at >= cutoff)
            .where(func.lower(CardIdentification.parsed_title) == parsed_title.lower())
            .group_by(IdentificationFeedback.chosen_card_id)
        )
        rows = (await db.execute(stmt)).all()
        # Normalise: 1 prior hit → 0.4, 3+ → 1.0 (asymptote). Avoids a single
        # rogue confirmation from dominating ranking.
        priors: dict[str, float] = {}
        for chosen_card_id, hits in rows:
            if not chosen_card_id:
                continue
            value = min(1.0, 0.4 + (int(hits) - 1) * 0.3)
            priors[chosen_card_id] = value
        return priors

    @staticmethod
    def _to_candidate(
        cand: dict[str, Any], source: str, breakdown: ScoreBreakdown
    ) -> CandidateOut:
        set_obj = cand.get("set") or {}
        images = cand.get("images") or {}
        image_url = (
            images.get("large")
            or images.get("normal")
            or images.get("small")
            or cand.get("image_url")
        )
        return CandidateOut(
            card_id=cand.get("card_id"),
            upstream_id=cand.get("id"),
            name=cand.get("name") or "",
            set_name=set_obj.get("name") or cand.get("set_name"),
            set_code=set_obj.get("code") or set_obj.get("id"),
            number=cand.get("number") or set_obj.get("number"),
            image_url=image_url if isinstance(image_url, str) else None,
            tcg=cand.get("tcg"),
            confidence=breakdown.final,
            source=source,
            breakdown={
                "text_similarity": breakdown.text_similarity,
                "ocr_quality": breakdown.ocr_quality,
                "field_match": breakdown.field_match,
                "phash_match": breakdown.phash_match,
                "feedback_prior": breakdown.feedback_prior,
            },
        )

    async def _persist(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID | None,
        prepared: PreparedImage,
        ocr_result: OcrResult,
        parsed: ParsedCard,
        tcg_inferred: str,
        candidates: list[CandidateOut],
        accuracy: float,
        primary_source: str,
        latency_ms: int,
        cost_usd: float,
    ) -> uuid.UUID:
        from app.models.identification import CardIdentification

        top = candidates[0] if candidates else None
        row = CardIdentification(
            user_id=user_id,
            image_sha256=prepared.sha256,
            phash=prepared.fingerprint.phash if prepared.fingerprint else None,
            ocr_provider=ocr_result.provider,
            ocr_full_text=(ocr_result.full_text or "")[:8000],
            ocr_confidence=ocr_result.mean_confidence,
            parsed_title=(parsed.title or "")[:200] or None,
            parsed_set_code=parsed.set_code,
            parsed_card_number=parsed.card_number,
            tcg_inferred=tcg_inferred,
            primary_source=primary_source,
            top_card_id=top.card_id if top else None,
            top_upstream_id=top.upstream_id if top else None,
            top_confidence=accuracy,
            candidates_json=[
                {
                    "card_id": c.card_id,
                    "upstream_id": c.upstream_id,
                    "name": c.name,
                    "confidence": c.confidence,
                    "source": c.source,
                    "breakdown": c.breakdown,
                }
                for c in candidates
            ],
            latency_ms=latency_ms,
            cost_usd=cost_usd,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


__all__ = ["CandidateOut", "CardIdentifier", "IdentifyOutcome"]
