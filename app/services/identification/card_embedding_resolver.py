"""Nearest-neighbour card lookup over the pgvector embedding index.

Given a query image's CNN embedding, find the closest catalog card by cosine
distance (pgvector ``<=>``). This is the learned-embedding counterpart to the
pHash resolver — robust to distance / blur / glare. Isolated in its own module
(NOT imported by the live ``card_identifier`` pipeline yet) so shipping it can
never affect the working scanner; wire it into identify as a follow-up once the
model is hosted and the catalog is back-filled.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("services.identification.embedding_resolver")


@dataclass(slots=True)
class EmbeddingMatch:
    card_id: str
    distance: float  # cosine distance in [0, 2]; lower = closer
    confidence: float  # 1 - distance, clamped to [0, 1]


async def resolve_by_embedding(
    db: AsyncSession,
    vector: list[float],
    *,
    tcg_hint: str | None = None,
    max_distance: float | None = None,
) -> EmbeddingMatch | None:
    """Best catalog card for ``vector`` within the cosine-distance ceiling.

    Returns ``None`` when embeddings are disabled, the vector is empty, or no
    catalog row is close enough. Postgres/pgvector only — a no-op elsewhere.
    """
    settings = get_settings()
    if not settings.embeddings_identify_enabled or not vector:
        return None
    if db.bind is not None and db.bind.dialect.name != "postgresql":
        return None

    ceiling = settings.card_embed_max_distance if max_distance is None else max_distance
    literal = "[" + ",".join(f"{x:.6f}" for x in vector) + "]"

    sql = """
        SELECT e.card_id, (e.embedding <=> CAST(:vec AS vector)) AS distance
        FROM catalog_card_embeddings e
        JOIN catalog_mirror_cards c ON c.id = e.card_id
        WHERE (:tcg IS NULL OR c.tcg = :tcg)
        ORDER BY e.embedding <=> CAST(:vec AS vector)
        LIMIT 1
    """
    try:
        row = (
            await db.execute(text(sql), {"vec": literal, "tcg": tcg_hint})
        ).first()
    except Exception as exc:  # pragma: no cover - infra-dependent
        logger.warning("embedding resolve failed (%s)", exc)
        return None

    if row is None:
        return None
    card_id, distance = row[0], float(row[1])
    if distance > ceiling:
        return None
    return EmbeddingMatch(
        card_id=card_id,
        distance=distance,
        confidence=max(0.0, min(1.0, 1.0 - distance)),
    )


__all__ = ["EmbeddingMatch", "resolve_by_embedding"]
