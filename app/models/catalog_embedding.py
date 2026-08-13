"""CatalogCardEmbedding — the pgvector art index the learned matcher reads.

One L2-normalised CNN image embedding per catalog card, backfilled by
``scripts/backfill_embeddings.py`` and queried by
``app/services/identification/card_embedding_resolver.py`` for Collectr-style
far/blurry recognition (cosine nearest neighbour, pgvector ``<=>``).

WHY THIS MODEL EXISTS AT ALL. The table shipped as raw SQL inside migration
0037 because nothing here had a column type for ``vector(N)`` or a way to spell
an ivfflat index. The cost of that was invisible and expensive: a table with no
model is not in ``Base.metadata``, so it was absent from every database this
repo actually builds — the whole SQLite suite, and the ``alembic upgrade
0001_initial`` bootstrap (0001 is ``create_all``). Only long-lived environments
migrated revision-by-revision had one, which made the embedding matcher a
feature that worked in production and nowhere else. Declaring it here costs a
dependency on ``pgvector.sqlalchemy`` and buys back a schema that is true
everywhere.

DIALECTS. The DDL below is byte-for-byte what 0037 emits on postgres, so a
migrated database and a ``create_all`` one converge rather than drift. On
SQLite (the 2,089-test suite) ``VECTOR(512)`` is just an unrecognised type
name — SQLite stores whatever it is handed — and the ivfflat qualifiers are
dropped, leaving a plain index. That is harmless because nothing on SQLite
writes or queries this table: the resolver returns early on any non-postgres
dialect. The table being *present* is the point; it is what stops the parity
tests, and the next reader, from believing the models are the whole schema.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DDL, DateTime, ForeignKey, Index, String, event, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: Output width of the CNN encoder. Duplicated from ``Settings.card_embed_dim``
#: (and from 0037's ``EMBED_DIM``) on purpose: a column width is DDL, and DDL
#: that changes with an environment variable would let two deployments of the
#: same revision have incompatible tables. Change it here, in 0037's constant
#: and in the setting together, with a migration to rewrite the column.
EMBED_DIM = 512


class CatalogCardEmbedding(Base):
    """One card's art embedding, keyed by the mirror card it describes."""

    __tablename__ = "catalog_card_embeddings"

    #: Composite catalog id (``pokemontcg:me4-1``) — PK, so the backfill
    #: upserts idempotently and a card can hold exactly one embedding.
    #: CASCADE because an embedding of a card that no longer exists in the
    #: mirror is unmatchable noise, not history worth keeping.
    card_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("catalog_mirror_cards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: L2-normalised embedding; cosine distance is the match score.
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM), nullable=False)
    #: Encoder identity (e.g. ``clip-vit-b32-v1``). Embeddings from different
    #: models are not comparable, so this is what a re-backfill keys off.
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # IVFFlat cosine index — approximate NN, the reason the query is fast
        # once the catalog is back-filled. lists=100 suits ~20k rows; retune
        # with catalog growth (the rule of thumb is rows/1000).
        #
        # Name pinned to 0037's so a migrated database and a create_all one
        # end up with the SAME index rather than two under different names.
        #
        # NOTE ON THE ONE THING THE ORM CANNOT SAY: ivfflat wants to be built
        # AFTER the rows exist — an index created on an empty table has no
        # centroids to learn from and postgres says so with a NOTICE. Neither
        # create_all nor a migration can express "build this later", so the
        # backfill script is what has to REINDEX afterwards; that ordering
        # lives in ops, not in the schema.
        Index(
            "ix_card_embeddings_cosine",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"lists": 100},
        ),
    )


# The `vector` type does not exist until the extension is installed, so a
# create_all against a fresh postgres would fail on this table with an
# unknown-type error that reads like a model bug. 0037 installs it for the
# migration path; this does the same for the create_all path (the bootstrap,
# and the postgres test harness). IF NOT EXISTS returns before any privilege
# check, so it is a no-op on every database that already has it — the tradeoff
# is that on a database that does NOT, the role now needs rights to create an
# extension, and the failure says exactly that instead of "type vector does
# not exist".
event.listen(
    CatalogCardEmbedding.__table__,
    "before_create",
    DDL("CREATE EXTENSION IF NOT EXISTS vector").execute_if(dialect="postgresql"),
)


__all__ = ["EMBED_DIM", "CatalogCardEmbedding"]
