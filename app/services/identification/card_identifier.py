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
from collections.abc import Awaitable
from dataclasses import dataclass, replace
from datetime import UTC
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.catalog import (
    card_resolver_service,
    card_search_service,
    catalog_hash_index,
)
from app.services.catalog.card_fingerprint_service import (
    fingerprint_variants_from_image_bytes,
)
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

_T = TypeVar("_T")

# Wall-clock ceiling for the whole upstream catalog-search phase of one
# identify (Pass 1 precise + Pass 2 name searches combined). Each provider call
# is separately capped, but on a genuine miss they stack; this keeps the total
# bounded so identify never rides Cloud Run's 60s request timeout into a 504.
_UPSTREAM_TOTAL_BUDGET_S = 9.0


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
        phash_str = prepared.fingerprint.phash if prepared.fingerprint else None

        # Step 1.5 — pHash fast path. The single biggest speed (and cost) win:
        # match the frame PLUS camera-correction variants (counter-rotations
        # for hand tilt, center-crops for loose framing) against the full
        # catalog art index. A decisive hit — near-exact, or margin-accepted
        # with dHash agreement — settles the card's identity with no OCR at
        # all. A weaker hit is reused below as the pipeline's pHash candidate.
        # The canonical frame fingerprint LEADS (hashed from the orientation-
        # normalized image — the exact same normalization the catalog index
        # used, so a clean frame matches at distance ~0). The variants add
        # camera-correction candidates on top; ocr_bytes is autocontrasted so
        # its base hash can drift a few bits — never let it displace the lead.
        fingerprints: list[tuple[str, str | None]] = []
        if prepared.fingerprint and prepared.fingerprint.phash:
            fingerprints.append(
                (prepared.fingerprint.phash, prepared.fingerprint.dhash)
            )
        variants = await asyncio.to_thread(
            fingerprint_variants_from_image_bytes, prepared.ocr_bytes
        )
        fingerprints.extend((fp.phash, fp.dhash) for fp in variants if fp.phash)
        catalog_phash = await self._resolve_catalog_best(db, fingerprints, tcg_hint)
        if (
            catalog_phash is not None
            and catalog_phash.unified is not None
            and catalog_phash.decisive
        ):
            return await self._phash_fast_outcome(
                db,
                prepared=prepared,
                resolved=catalog_phash,
                tcg_hint=tcg_hint,
                user_id=user_id,
                started=started,
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

        # Steps 5+6 — gather candidates from text search + phash.
        # The catalog pHash was already resolved above (Step 1.5); reuse it
        # rather than scanning the catalog twice. Only fall back to the
        # user-submission fingerprint index when the catalog had no match.
        text_task = self._search_text(db, parsed=parsed, tcg=tcg_inferred)
        if catalog_phash is not None and catalog_phash.unified is not None:
            text_candidates = await text_task
            phash_candidate: dict[str, Any] | None = catalog_phash.unified
        else:
            text_candidates, phash_candidate = await asyncio.gather(
                text_task,
                self._search_user_phash(db, phash_str),
                return_exceptions=False,
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
        scored: list[tuple[CandidateOut, ScoreBreakdown, tuple[int, int, str]]] = []
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
            scored.append(
                (
                    self._to_candidate(cand, source, breakdown),
                    breakdown,
                    self._canonical_rank_key(cand),
                )
            )
        top_candidates = self._rank(scored, settings.ocr_max_candidates)

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
        text_candidates = await self._search_text(db, parsed=parsed, tcg=tcg_inferred)
        merged = self._dedupe(text_candidates, None)

        feedback_priors: dict[str, float] = {}
        if parsed.title:
            try:
                feedback_priors = await self._feedback_priors(
                    db, ocr_text=ocr_result.full_text, parsed_title=parsed.title
                )
            except Exception:
                logger.exception("feedback prior lookup failed (text path)")

        scored: list[tuple[CandidateOut, ScoreBreakdown, tuple[int, int, str]]] = []
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
            scored.append(
                (
                    self._to_candidate(cand, source, breakdown),
                    breakdown,
                    self._canonical_rank_key(cand),
                )
            )
        top_candidates = self._rank(scored, settings.ocr_max_candidates)

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
        self, db: AsyncSession, *, parsed: ParsedCard, tcg: str
    ) -> list[dict[str, Any]]:
        """Candidates for the OCR'd text — LOCAL index first, upstream fallback.

        The denormalized art-hash index holds name/set/number for every
        catalog card, so an in-memory scan answers in milliseconds what the
        upstream catalog search answers in seconds (and sometimes not at all —
        it intermittently returns empty pages). Upstream remains the fallback
        for anything the index hasn't covered yet.
        """
        if not parsed.title_candidates and not parsed.title:
            return []
        seen: dict[str, dict[str, Any]] = {}
        titles = [t for t, _ in parsed.title_candidates] or [parsed.title or ""]
        titles = [t for t in titles if t]
        if not titles:
            return []

        try:
            local = await catalog_hash_index.search_text(
                db,
                tcg=tcg,
                titles=titles,
                number=parsed.card_number,
                limit=10,
            )
        except Exception:
            logger.exception("local index text search failed")
            local = []
        if local:
            return local

        def _merge(results: list[dict[str, Any]] | None) -> None:
            for result in results or []:
                key = (
                    result.get("id")
                    or f"{result.get('name')}::{(result.get('set') or {}).get('id')}"
                )
                if key and key not in seen:
                    seen[key] = result

        # Everything below hits the network. Each call is already capped at
        # PER_PROVIDER_TIMEOUT, but on a genuine miss they STACK — a precise
        # pass plus a name search (x2 retry) for each of up to 3 titles can
        # ride 30s+ under a slow upstream, which is exactly how identify used
        # to blow past Cloud Run's 60s request ceiling and 504. Cap the whole
        # upstream phase with one wall-clock budget: on expiry we return
        # whatever matched so far (usually nothing → a fast, clean no-match)
        # instead of hanging the scan.
        try:
            async with asyncio.timeout(_UPSTREAM_TOTAL_BUDGET_S):
                # Pass 1 — field-aware precise lookup. When OCR gave us a
                # collector number, pin it so the exact printing surfaces even
                # if a name-only search would bury it under promos (e.g. Base
                # Set Pikachu #58 never appears in the top 10 of a plain
                # "Pikachu" search). One upstream call; degrades to [] on
                # failure so we still keep the fuzzy hits.
                if parsed.card_number:
                    precise = await self._catalog_lookup(
                        "precise",
                        card_search_service.search_cards_precise(
                            tcg=tcg,
                            name=titles[0],
                            number=parsed.card_number,
                            set_code=parsed.set_code,
                            limit=10,
                        ),
                        [],
                    )
                    _merge(precise)

                # Pass 2 — name-only fuzzy search for carousel breadth. Try the
                # top title; only fall through to runner-ups if nothing has
                # matched yet. Retry an empty response ONCE, but only for pure
                # name searches (no collector number): the upstream
                # intermittently returns an empty-but-successful page for a
                # valid name (a documented flake — see the public-browse
                # retry-on-empty). When we have a number we've already run a
                # precise lookup, so a second name search would just double
                # latency on a genuine miss.
                retry_empty = not parsed.card_number
                for title in titles:
                    body = await self._search_name_once(title, tcg)
                    if retry_empty and not body.get("results"):
                        body = await self._search_name_once(title, tcg)
                    _merge(body.get("results", []))
                    if seen:
                        break  # found hits — don't spend budget on backups
        except TimeoutError:
            logger.info(
                "identify upstream search hit the %.1fs total budget; "
                "returning %d partial hit(s)",
                _UPSTREAM_TOTAL_BUDGET_S,
                len(seen),
            )
        return list(seen.values())

    async def _search_name_once(self, title: str, tcg: str) -> dict[str, Any]:
        """One name-only catalog search, timeout-guarded → ``{"results": [...]}``."""
        return await self._catalog_lookup(
            "text",
            card_search_service.search_cards(q=title, tcg=tcg, limit=10),
            {"results": []},
        )

    async def _catalog_lookup(
        self, label: str, awaitable: Awaitable[_T], default: _T
    ) -> _T:
        timeout = max(0.1, float(card_search_service.PER_PROVIDER_TIMEOUT))
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except TimeoutError:
            logger.info(
                "identify catalog %s lookup timed out after %.1fs", label, timeout
            )
        except Exception:
            logger.exception("identify catalog %s lookup failed", label)
        return default

    async def _resolve_catalog_best(
        self,
        db: AsyncSession,
        fingerprints: list[tuple[str, str | None]],
        tcg_hint: str | None,
    ) -> card_resolver_service.ResolvedCard | None:
        """Best catalog art-hash match for the frame + its camera-correction
        variants (with margin acceptance and dHash cross-check).

        Returns the full :class:`ResolvedCard`; ``decisive=True`` means the
        identity is settled and the caller may skip OCR. ``None`` when pHash
        is disabled or nothing plausible matched.
        """
        if not fingerprints or not get_settings().phash_enabled:
            return None
        try:
            return await card_resolver_service.resolve_catalog_best(
                db, fingerprints, tcg=tcg_hint
            )
        except Exception:
            logger.exception("catalog phash resolve failed")
            return None

    async def _search_user_phash(
        self, db: AsyncSession, phash: str | None
    ) -> dict[str, Any] | None:
        """Fallback pHash match against user-submission fingerprints.

        Only consulted when the catalog art hashes miss — lets a card a user
        has graded before be recognised even if it isn't in the catalog index.
        """
        if not phash or not get_settings().phash_enabled:
            return None
        try:
            resolved = await card_resolver_service.resolve_by_phash(db, phash)
        except Exception:
            logger.exception("user phash resolve failed")
            return None
        return resolved.unified if resolved and resolved.unified else None

    async def _phash_fast_outcome(
        self,
        db: AsyncSession,
        *,
        prepared: PreparedImage,
        resolved: card_resolver_service.ResolvedCard,
        tcg_hint: str | None,
        user_id: uuid.UUID | None,
        started: float,
    ) -> IdentifyOutcome:
        """Build + persist an identification from a near-exact pHash match,
        with no OCR. The card's identity is settled by the art hash."""
        unified = resolved.unified or {}
        parsed = parse_ocr_text("")  # no OCR ran — empty parse
        breakdown = score_candidate(
            parsed=parsed,
            candidate=unified,
            ocr_confidence=0.0,
            phash_hit=True,
            feedback_prior=0.0,
        )
        candidate = self._to_candidate(unified, "phash", breakdown)
        # The art-hash identity is the signal here; trust the resolver's
        # confidence when it's stronger than the generic scorer (which has no
        # OCR text to lean on).
        candidate = replace(
            candidate,
            confidence=round(max(candidate.confidence, resolved.confidence), 3),
        )
        ocr_result = OcrResult(
            full_text="",
            blocks=[],
            mean_confidence=0.0,
            language_codes=[],
            provider="phash_fast_path",
            latency_ms=0,
        )
        tcg_inferred = tcg_hint or candidate.tcg or "unknown"
        latency_ms = int((time.perf_counter() - started) * 1000)
        identification_id = await self._persist(
            db,
            user_id=user_id,
            prepared=prepared,
            ocr_result=ocr_result,
            parsed=parsed,
            tcg_inferred=tcg_inferred,
            candidates=[candidate],
            accuracy=candidate.confidence,
            primary_source="phash",
            latency_ms=latency_ms,
            cost_usd=0.0,
        )
        return IdentifyOutcome(
            identification_id=identification_id,
            candidates=[candidate],
            accuracy_score=candidate.confidence,
            primary_source="phash",
            parsed=parsed,
            ocr=ocr_result,
            latency_ms=latency_ms,
            tcg_inferred=tcg_inferred,
            cost_usd=0.0,
        )

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
    def _canonical_rank_key(cand: dict[str, Any]) -> tuple[int, int, str]:
        """Deterministic tie-break for equal-confidence candidates.

        Several printings can share a name AND collector number — Charizard
        ``4/102`` exists in Base Set (1999), Base Set 2 (2000), and Legendary
        Collection (2002). The weighted score ties them, so returning an
        arbitrary one (whatever order the upstream API happened to give) made
        the scanner feel like it "picked a random cheap reprint". We instead
        prefer the ORIGINAL printing: earliest release year, then the smaller
        set, then a stable id. A clean-image pHash hit still outranks this via
        its confidence bonus, so this only ever decides genuine ties.
        """
        set_obj = cand.get("set") or {}
        year = cand.get("year")
        year_key = year if isinstance(year, int) and 1900 < year < 2100 else 9999
        total_raw = (
            set_obj.get("printed_total") or set_obj.get("total_cards") or 1_000_000
        )
        try:
            total_key = int(total_raw)
        except (TypeError, ValueError):
            total_key = 1_000_000
        return (year_key, total_key, str(cand.get("id") or ""))

    @staticmethod
    def _rank(
        scored: list[tuple[CandidateOut, ScoreBreakdown, tuple[int, int, str]]],
        limit: int,
    ) -> list[CandidateOut]:
        """Confidence desc, ties broken by the canonical printing key."""
        scored.sort(key=lambda t: (-t[0].confidence, t[2]))
        return [t[0] for t in scored[:limit]]

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
            image_thumb_b64=prepared.thumb_b64,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


__all__ = ["CandidateOut", "CardIdentifier", "IdentifyOutcome"]
