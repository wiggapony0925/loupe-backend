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

from app.cache_config import SCAN_PUBSUB_CHANNEL
from app.clients.redis_client import get_redis
from app.db import get_sessionmaker
from app.models.card import Card
from app.models.enums import GradeHouseEnum, ScanStatusEnum, TcgEnum
from app.models.fingerprint import Fingerprint
from app.models.grade import GradedCard
from app.models.scan import ScanJob
from app.schemas.scan import ScanProgressEvent
from app.services.fingerprint_service import fingerprint_from_images
from app.services.grading_service import grade_from_images
from app.utils.logger import get_logger
from app.utils.time import utcnow
from app.ws_manager import get_manager

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

    placeholder = await _ensure_placeholder_card(db)
    graded = GradedCard(
        user_id=user_id,
        card_id=placeholder.id,
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
    if db is not None:
        await _process(db, job_id, user_id)
    else:  # pragma: no cover - exercised under arq only
        sm = get_sessionmaker()
        async with sm() as session:
            await _process(session, job_id, user_id)
    return {"job_id": str(job_id), "status": "ok"}


async def arq_process_scan(
    ctx: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Arq adapter that hands off to :func:`process_scan` with a new session."""
    payload_str = json.dumps(payload)
    logger.info("Worker received scan: %s", payload_str)
    return await process_scan(payload)


__all__ = ["arq_process_scan", "process_scan"]
