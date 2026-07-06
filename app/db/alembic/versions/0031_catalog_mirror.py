"""Local catalog mirror + durable kv cache.

``catalog_mirror_sets`` / ``catalog_mirror_cards`` hold our own complete copy
of upstream card catalogs (Pokémon first) so browse/search/detail are served
from Postgres in milliseconds instead of live-proxying a flaky upstream.
``kv_cache`` is the durable L2 cache tier behind Redis / in-process cache —
production Cloud Run has no reachable Redis, so without it every instance
recycle wiped all cached catalog data.

Additive — safe online migration.

Revision ID: 0031_catalog_mirror
Revises: 0030_catalog_hash_alt
Create Date: 2026-07-06 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_catalog_mirror"
down_revision: str | Sequence[str] | None = "0030_catalog_hash_alt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_mirror_sets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("tcg", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("series", sa.String(200), nullable=True),
        sa.Column("release_date", sa.String(20), nullable=True),
        sa.Column("printed_total", sa.Integer, nullable=True),
        sa.Column("total", sa.Integer, nullable=True),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("card_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prices_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_catalog_mirror_sets_source", "catalog_mirror_sets", ["source"]
    )
    op.create_index("ix_catalog_mirror_sets_tcg", "catalog_mirror_sets", ["tcg"])
    op.create_index(
        "ix_catalog_mirror_sets_release_date",
        "catalog_mirror_sets",
        ["release_date"],
    )

    op.create_table(
        "catalog_mirror_cards",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("tcg", sa.String(32), nullable=False),
        sa.Column("upstream_id", sa.String(96), nullable=False),
        sa.Column("set_id", sa.String(64), nullable=False),
        sa.Column("set_name", sa.String(200), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("name_lower", sa.String(200), nullable=False),
        sa.Column("number", sa.String(40), nullable=True),
        sa.Column("bare_number", sa.String(20), nullable=True),
        sa.Column("number_int", sa.Integer, nullable=True),
        sa.Column("rarity", sa.String(60), nullable=True),
        sa.Column("release_date", sa.String(20), nullable=True),
        sa.Column("sort_price", sa.Float, nullable=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_catalog_mirror_cards_source", "catalog_mirror_cards", ["source"]
    )
    op.create_index("ix_catalog_mirror_cards_tcg", "catalog_mirror_cards", ["tcg"])
    op.create_index(
        "ix_catalog_mirror_cards_set_id", "catalog_mirror_cards", ["set_id"]
    )
    op.create_index(
        "ix_catalog_mirror_cards_name_lower", "catalog_mirror_cards", ["name_lower"]
    )
    op.create_index(
        "ix_catalog_mirror_cards_bare_number",
        "catalog_mirror_cards",
        ["bare_number"],
    )
    op.create_index(
        "ix_catalog_mirror_cards_release_date",
        "catalog_mirror_cards",
        ["release_date"],
    )
    op.create_index(
        "ix_catalog_mirror_cards_set_order",
        "catalog_mirror_cards",
        ["set_id", "number_int"],
    )
    op.create_index(
        "ix_catalog_mirror_cards_newest",
        "catalog_mirror_cards",
        ["tcg", "release_date"],
    )
    op.create_index(
        "ix_catalog_mirror_cards_price",
        "catalog_mirror_cards",
        ["tcg", "sort_price"],
    )

    op.create_table(
        "kv_cache",
        sa.Column("key", sa.String(512), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_kv_cache_expires_at", "kv_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_kv_cache_expires_at", table_name="kv_cache")
    op.drop_table("kv_cache")
    for name in (
        "ix_catalog_mirror_cards_price",
        "ix_catalog_mirror_cards_newest",
        "ix_catalog_mirror_cards_set_order",
        "ix_catalog_mirror_cards_release_date",
        "ix_catalog_mirror_cards_bare_number",
        "ix_catalog_mirror_cards_name_lower",
        "ix_catalog_mirror_cards_set_id",
        "ix_catalog_mirror_cards_tcg",
        "ix_catalog_mirror_cards_source",
    ):
        op.drop_index(name, table_name="catalog_mirror_cards")
    op.drop_table("catalog_mirror_cards")
    for name in (
        "ix_catalog_mirror_sets_release_date",
        "ix_catalog_mirror_sets_tcg",
        "ix_catalog_mirror_sets_source",
    ):
        op.drop_index(name, table_name="catalog_mirror_sets")
    op.drop_table("catalog_mirror_sets")
