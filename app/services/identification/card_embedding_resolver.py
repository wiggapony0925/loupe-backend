"""Nearest-neighbour card lookup over the pgvector embedding index.

Given a query image's CNN embedding, find the closest catalog card by cosine
distance (pgvector ``<=>``). This is the learned-embedding counterpart to the
pHash resolver — robust to distance / blur / glare. Isolated in its own module
(NOT imported by the live ``card_identifier`` pipeline yet) so shipping it can
never affect the working scanner; wire it into identify as a follow-up once the
model is hosted and the catalog is back-filled.

FAILING LOUDLY. Every reason this resolver can decline to answer is a
*degradation* — identification falls back to pHash + OCR and the scan still
succeeds — which is exactly why the reasons must be told apart. Two of them are
normal and silent (the flag is off, the dialect is not postgres). One is
transient and logged as a warning (the database blipped mid-query). The rest
are deployment or wiring faults — no embeddings table, a vector of the wrong
width — and those are logged at ERROR, once, naming the cause, because their
only other symptom is a match rate that is quietly worse than it should be.
This module used to wrap the query in ``except Exception`` and log a warning,
so a database with no embeddings table (the whole test suite, and any
environment bootstrapped with ``create_all`` rather than the full migration
chain) produced a matcher that never matched and never said why.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.catalog_embedding import EMBED_DIM
from app.utils.logger import get_logger

logger = get_logger("services.identification.embedding_resolver")

#: Per-process answer to "does this database have the embeddings table?".
#: ``None`` until asked. Cached because the answer cannot change under a
#: running process without a deploy, and a schema probe on every scan would be
#: a round-trip bought for nothing. The same cache is what makes the missing
#: table log ERROR *once* instead of once per scan.
_table_present: bool | None = None


@dataclass(slots=True)
class EmbeddingMatch:
    card_id: str
    distance: float  # cosine distance in [0, 2]; lower = closer
    confidence: float  # 1 - distance, clamped to [0, 1]


def reset_schema_probe() -> None:
    """Forget the cached table probe. For tests and for a reconnecting worker."""
    global _table_present
    _table_present = None


async def _embedding_table_present(db: AsyncSession) -> bool:
    """Is ``catalog_card_embeddings`` actually in this database?

    Asked with ``to_regclass``, which answers NULL for a missing relation
    instead of raising — deliberately, because a failed statement aborts the
    caller's transaction, and a matcher that silently poisons the identify
    request's session would trade one quiet bug for a much worse one.
    """
    global _table_present
    if _table_present is not None:
        return _table_present

    try:
        found = (
            await db.execute(text("SELECT to_regclass('catalog_card_embeddings')"))
        ).scalar()
    except (OperationalError, InterfaceError) as exc:
        # The server is unreachable; that says nothing about the schema, so
        # answer "no" for this scan without caching a conclusion we did not
        # reach — the next scan asks again.
        logger.warning("embedding table probe failed, skipping matcher (%s)", exc)
        return False

    _table_present = found is not None
    if not _table_present:
        logger.error(
            "catalog_card_embeddings is missing from this database: migration "
            "0037 has not been applied here. The learned-embedding matcher is "
            "disabled for this process and identification falls back to "
            "pHash + OCR — scans still succeed, with a worse match rate."
        )
    return _table_present


async def resolve_by_embedding(
    db: AsyncSession,
    vector: list[float],
    *,
    tcg_hint: str | None = None,
    max_distance: float | None = None,
) -> EmbeddingMatch | None:
    """Best catalog card for ``vector`` within the cosine-distance ceiling.

    Returns ``None`` when embeddings are disabled, the vector is empty or the
    wrong width, the embeddings table is absent, or no catalog row is close
    enough. Postgres/pgvector only — a no-op elsewhere.
    """
    settings = get_settings()
    if not settings.embeddings_identify_enabled or not vector:
        return None
    if db.bind is not None and db.bind.dialect.name != "postgresql":
        return None

    # Width is checked here rather than left to postgres because pgvector
    # rejects a mismatch with a DataError *inside* the caller's transaction,
    # which aborts it — a wrong-width vector would then fail the next query in
    # the identify request rather than this one. A mismatch means the encoder
    # and the column have drifted apart (see Settings.card_embed_dim), so it is
    # a wiring fault worth shouting about, not a per-scan condition.
    if len(vector) != EMBED_DIM:
        logger.error(
            "embedding is %d-dimensional but catalog_card_embeddings holds "
            "vector(%d) — encoder and schema disagree; matcher skipped",
            len(vector),
            EMBED_DIM,
        )
        return None

    if not await _embedding_table_present(db):
        return None

    ceiling = settings.card_embed_max_distance if max_distance is None else max_distance
    literal = "[" + ",".join(f"{x:.6f}" for x in vector) + "]"

    # The tcg filter is spliced in rather than written as `:tcg IS NULL OR
    # c.tcg = :tcg`, which is the idiomatic form and does not work here:
    # asyncpg PREPARES the statement server-side, and postgres cannot infer a
    # type for a parameter it first meets in `$2 IS NULL` — it answers
    # AmbiguousParameterError and the query never runs. psycopg2 interpolates
    # client-side and would have been fine, which is why the shape survived.
    # Two statements is also the better plan: no per-row NULL test.
    params: dict[str, object] = {"vec": literal}
    where = ""
    if tcg_hint is not None:
        where = "WHERE c.tcg = :tcg"
        params["tcg"] = tcg_hint

    sql = f"""
        SELECT e.card_id, (e.embedding <=> CAST(:vec AS vector)) AS distance
        FROM catalog_card_embeddings e
        JOIN catalog_mirror_cards c ON c.id = e.card_id
        {where}
        ORDER BY e.embedding <=> CAST(:vec AS vector)
        LIMIT 1
    """
    try:
        row = (await db.execute(text(sql), params)).first()
    except (OperationalError, InterfaceError) as exc:
        # The only genuinely recoverable class: connection lost, statement
        # timed out, server restarting. The scan degrades to its other paths.
        # Anything else — a syntax error, a type that vanished, a constraint —
        # is a bug, and propagating it is how it gets found; the gates above
        # already handle the two faults that are merely a bad deploy.
        logger.warning("embedding resolve failed, skipping matcher (%s)", exc)
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


__all__ = ["EmbeddingMatch", "reset_schema_probe", "resolve_by_embedding"]
