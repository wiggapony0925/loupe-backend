"""card art embeddings (pgvector) for the learned-embedding matcher

Adds a `catalog_card_embeddings` table holding one L2-normalised CNN image
embedding per catalog card, for Collectr-style far/blurry recognition via
pgvector nearest-neighbour. POSTGRES-ONLY: the `vector` type + extension only
exist on Postgres, so on SQLite (tests) this is a no-op — the whole embedding
path is flag-gated off by default and falls back to pHash+OCR regardless.

Revision ID: 0037_card_embeddings
Revises: 0036_pricecharting_prices
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0037_card_embeddings"
down_revision: str | Sequence[str] | None = "0036_pricecharting_prices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in sync with Settings.card_embed_dim.
EMBED_DIM = 512


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # vector type is Postgres-only; SQLite tests skip this.

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS catalog_card_embeddings (
            card_id    VARCHAR(128) PRIMARY KEY
                       REFERENCES catalog_mirror_cards(id) ON DELETE CASCADE,
            embedding  vector({EMBED_DIM}) NOT NULL,
            model      VARCHAR(64) NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # IVFFlat cosine index — fast approximate NN once the table is back-filled.
    # (lists=100 is a sane default for ~20k rows; retune with catalog growth.)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_card_embeddings_cosine "
        "ON catalog_card_embeddings USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP TABLE IF EXISTS catalog_card_embeddings")
