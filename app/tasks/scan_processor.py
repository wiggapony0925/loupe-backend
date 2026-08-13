"""Scan-processing background task.

Loads the scan, runs the grading + fingerprint pipeline against the uploaded
images, persists the GradedCard + Fingerprint records, and publishes progress
events on the per-user Redis channel.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.models.card import Card
from app.models.enums import GradeHouseEnum, ScanStatusEnum, TcgEnum
from app.models.fingerprint import Fingerprint
from app.models.grade import GradedCard
from app.models.scan import ScanJob
from app.platform.cache_config import SCAN_PUBSUB_CHANNEL
from app.platform.redis_client import get_redis
from app.platform.ws_manager import get_manager
from app.schemas.scan import ScanProgressEvent
from app.services.catalog import card_resolver_service
from app.services.catalog.card_fingerprint_service import fingerprint_from_images
from app.services.collection.grading_service import grade_from_images
from app.utils.logger import get_logger
from app.utils.time import utcnow

logger = get_logger("workers.scan")


async def _publish(user_id: uuid.UUID, event: ScanProgressEvent) -> None:
    """Broadcast a progress event via in-process WS manager + Redis pub/sub."""
    payload = event.model_dump(mode="json")
    await get_manager().broadcast(str(user_id), payload)
    redis = await get_redis()
    if hasattr(redis, "publish"):
        try:
            await redis.publish(
                SCAN_PUBSUB_CHANNEL.format(user_id=user_id), event.model_dump_json()
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("Redis publish failed: %s", exc)


async def _ensure_placeholder_card(db: AsyncSession) -> Card:
    """Best-effort placeholder card used when the grader can't identify the card.

    Neither `Card.name` nor `CardSet.name` is unique, so two concurrent
    scan jobs can race and each insert their own "Unidentified" row.
    We use `LIMIT 1` (and `.first()`) so the next lookup deterministically
    picks one survivor instead of crashing with `MultipleResultsFound`.
    """
    placeholder_name = "Unidentified Loupe Capture"
    existing = (
        (await db.execute(select(Card).where(Card.name == placeholder_name).limit(1)))
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    from app.models.card import CardSet

    placeholder_set = (
        (
            await db.execute(
                select(CardSet).where(CardSet.name == "Unidentified Set").limit(1)
            )
        )
        .scalars()
        .first()
    )
    if placeholder_set is None:
        placeholder_set = CardSet(tcg=TcgEnum.pokemon, name="Unidentified Set")
        db.add(placeholder_set)
        await db.flush()
    card = Card(set_id=placeholder_set.id, tcg=TcgEnum.pokemon, name=placeholder_name)
    db.add(card)
    await db.flush()
    return card


async def _process(db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID) -> None:
    job = (
        await db.execute(
            select(ScanJob).where(ScanJob.id == job_id, ScanJob.user_id == user_id)
        )
    ).scalar_one_or_none()
    if job is None:
        logger.warning("Scan job %s for user %s not found", job_id, user_id)
        return
    if job.status not in (ScanStatusEnum.processing, ScanStatusEnum.uploading):
        logger.debug("Skipping scan %s in status %s", job.id, job.status)
        return

    job.status = ScanStatusEnum.processing
    if job.started_at is None:
        job.started_at = utcnow()
    await db.commit()
    await _publish(
        user_id,
        ScanProgressEvent(
            job_id=job.id, status=ScanStatusEnum.processing, progress=0.1
        ),
    )

    image_keys = dict(job.images_s3_keys or {})
    if not image_keys:
        job.status = ScanStatusEnum.failed
        job.error_message = "No images attached"
        job.completed_at = utcnow()
        await db.commit()
        await _publish(
            user_id,
            ScanProgressEvent(
                job_id=job.id,
                status=ScanStatusEnum.failed,
                progress=1.0,
                message="No images attached",
            ),
        )
        return

    grading = grade_from_images(image_keys)
    await _publish(
        user_id,
        ScanProgressEvent(
            job_id=job.id, status=ScanStatusEnum.processing, progress=0.6
        ),
    )
    fingerprint = fingerprint_from_images(image_keys)

    # Try to identify the scanned card before falling back to placeholder.
    # Order: pHash match against the existing catalog → text from the
    # grading pipeline if it surfaced an identification → placeholder.
    resolved_card: Card | None = None
    try:
        match = await card_resolver_service.resolve(
            db,
            phash=fingerprint.phash,
            query=getattr(grading, "identified_name", None),
            materialize=True,
        )
        if match and match.card_id is not None:
            from sqlalchemy import select as _select

            resolved_card = (
                await db.execute(_select(Card).where(Card.id == match.card_id))
            ).scalar_one_or_none()
    except Exception as exc:  # pragma: no cover - never fail a scan on resolve
        logger.info("scan %s resolve failed: %s", job.id, exc)

    card_for_grade = resolved_card or await _ensure_placeholder_card(db)

    # Idempotency guard — re-uploading the same physical photo (or an arq
    # retry of the same job) must NOT create a phantom duplicate row.
    # Owning multiple copies is intentional and goes through the manual
    # POST /v1/grades flow; identical-fingerprint scans from the same
    # user within a short window are treated as the same submission.
    from datetime import timedelta as _td

    dup_cutoff = utcnow() - _td(minutes=5)
    existing = (
        await db.execute(
            select(GradedCard)
            .where(
                GradedCard.user_id == user_id,
                GradedCard.card_id == card_for_grade.id,
                GradedCard.fingerprint_hash == fingerprint.phash,
                GradedCard.created_at >= dup_cutoff,
                GradedCard.deleted_at.is_(None),
            )
            .order_by(GradedCard.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing is not None:
        logger.info(
            "scan %s deduped onto existing graded_card %s (same user/card/phash within 5m)",
            job.id,
            existing.id,
        )
        graded = existing
        job.status = ScanStatusEnum.complete
        job.completed_at = utcnow()
        await db.commit()
        await db.refresh(graded)
    else:
        graded = GradedCard(
            user_id=user_id,
            card_id=card_for_grade.id,
            scan_job_id=job.id,
            grade=grading.overall,
            house=GradeHouseEnum.loupe,
            subgrades=grading.subgrades.as_dict(),
            fingerprint_hash=fingerprint.phash,
        )
        db.add(graded)
        await db.flush()
        db.add(
            Fingerprint(
                graded_card_id=graded.id,
                phash=fingerprint.phash,
                dhash=fingerprint.dhash,
                feature_vector={"v": fingerprint.feature_vector},
            )
        )
        job.status = ScanStatusEnum.complete
        job.completed_at = utcnow()
        await db.commit()
        await db.refresh(graded)

    await _publish(
        user_id,
        ScanProgressEvent(
            job_id=job.id,
            status=ScanStatusEnum.complete,
            progress=1.0,
            result={
                "graded_card_id": str(graded.id),
                "grade": float(graded.grade),
                "subgrades": graded.subgrades,
                "fingerprint": fingerprint.phash,
            },
        ),
    )


async def process_scan(
    payload: dict[str, Any], db: AsyncSession | None = None
) -> dict[str, Any]:
    """Arq task entrypoint.

    ``payload`` carries ``job_id`` + ``user_id``.  When invoked inline from the
    HTTP layer, ``db`` is supplied; when invoked by arq, a fresh session is
    opened.
    """
    job_id = uuid.UUID(str(payload["job_id"]))
    user_id = uuid.UUID(str(payload["user_id"]))
    try:
        if db is not None:
            await _process(db, job_id, user_id)
        else:  # pragma: no cover - exercised under arq only
            sm = get_sessionmaker()
            async with sm() as session:
                await _process(session, job_id, user_id)
    except Exception as exc:
        # Without this the job stays in `processing` forever. `_process` had no
        # guard, the router's caller swallows what escapes
        # (routers/collection/scans.py:62, "will rely on worker"), and there is
        # no reaper cron — worker.py schedules catalog_sync, price_backfill,
        # price_snapshot, image_index, pro_expiry and portfolio_digest, and
        # nothing that times out a stuck scan. The client polls for
        # complete-or-failed, so a crash left it waiting indefinitely with no
        # error to show. It also made the pipeline undiagnosable: a
        # systematically failing scan is indistinguishable from nobody
        # scanning, which is exactly the ambiguity the empty `error_message`
        # column left us with.
        logger.exception("scan %s crashed during processing", job_id)
        await _fail(job_id, user_id, exc)
        return {"job_id": str(job_id), "status": "failed"}
    return {"job_id": str(job_id), "status": "ok"}


async def _fail(job_id: uuid.UUID, user_id: uuid.UUID, exc: BaseException) -> None:
    """Mark a crashed job failed, in its own session.

    A fresh session on purpose: the exception may well have come from the
    caller's, leaving it mid-rollback, and the one write that must survive a
    database problem is the one that stops the client waiting.

    Guarded on status so a job that already reached a terminal state — or one
    the worker retries after a partial success — is never dragged backwards.
    """
    from sqlalchemy import update

    message = f"{type(exc).__name__}: {exc}"[:1024]
    try:
        async with get_sessionmaker()() as session:
            await session.execute(
                update(ScanJob)
                .where(
                    ScanJob.id == job_id,
                    ScanJob.status.in_(
                        [ScanStatusEnum.processing, ScanStatusEnum.uploading]
                    ),
                )
                .values(
                    status=ScanStatusEnum.failed,
                    error_message=message,
                    completed_at=utcnow(),
                )
            )
            await session.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("could not mark scan %s failed", job_id)

    await _publish(
        user_id,
        ScanProgressEvent(
            job_id=job_id,
            status=ScanStatusEnum.failed,
            progress=1.0,
            message="Scan failed. Please try again.",
        ),
    )


async def arq_process_scan(
    ctx: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Arq adapter that hands off to :func:`process_scan` with a new session."""
    payload_str = json.dumps(payload)
    logger.info("Worker received scan: %s", payload_str)
    return await process_scan(payload)


__all__ = ["arq_process_scan", "process_scan"]
