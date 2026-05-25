"""Create ``sealed_products`` + ``sealed_holdings`` tables.

Adds first-class support for sealed TCG product (booster boxes, ETBs,
tins, etc.) which collectors track alongside singles as long-hold
investments. The catalog row (``sealed_products``) is shared across
users; ownership lives in ``sealed_holdings`` and mirrors the
graded-card cost-basis shape so portfolio rollups can compose without
a translation layer.

Revision ID: 0008_sealed_products
Revises: 0007_grade_condition
Create Date: 2026-05-25 21:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.types import UuidCol

revision: str = "0008_sealed_products"
down_revision: str | Sequence[str] | None = "0007_grade_condition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TYPE_VALUES = (
    "booster_box",
    "booster_pack",
    "etb",
    "collection_box",
    "premium_collection",
    "tin",
    "blister",
    "bundle",
    "case",
    "other",
)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # Use postgresql.ENUM (not generic sa.Enum) so create_type=False is
        # honored during _on_table_create. The previous attempt with sa.Enum
        # silently re-issued CREATE TYPE during create_table, crashing with
        # DuplicateObject on retries where the type already exists (e.g.
        # after a partially-succeeded prior migration left the type in
        # place but rolled back the tables).
        postgresql.ENUM(
            *_TYPE_VALUES,
            name="sealed_product_type_enum",
            create_type=True,
        ).create(bind, checkfirst=True)
        product_type_col = postgresql.ENUM(
            *_TYPE_VALUES,
            name="sealed_product_type_enum",
            create_type=False,
        )
        tcg_col = postgresql.ENUM(
            "pokemon",
            "magic",
            "yugioh",
            "onepiece",
            "lorcana",
            "sports",
            name="tcg_enum",
            create_type=False,
        )
    else:
        product_type_col = sa.Enum(*_TYPE_VALUES, name="sealed_product_type_enum")
        tcg_col = sa.Enum(
            "pokemon",
            "magic",
            "yugioh",
            "onepiece",
            "lorcana",
            "sports",
            name="tcg_enum",
        )

    # ── sealed_products (catalog) ─────────────────────────────────────────
    op.create_table(
        "sealed_products",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "tcg",
            tcg_col,
            nullable=False,
        ),
        sa.Column("product_type", product_type_col, nullable=False),
        sa.Column(
            "set_id",
            UuidCol(),
            sa.ForeignKey("card_sets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("set_name", sa.String(length=200), nullable=True),
        sa.Column("image_url", sa.String(length=1024), nullable=True),
        sa.Column("msrp_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("release_date", sa.Date(), nullable=True),
        sa.Column("upstream_source", sa.String(length=40), nullable=True),
        sa.Column("upstream_id", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "upstream_source", "upstream_id", name="uq_sealed_products_upstream"
        ),
    )
    op.create_index("ix_sealed_products_tcg", "sealed_products", ["tcg"])
    op.create_index(
        "ix_sealed_products_product_type", "sealed_products", ["product_type"]
    )
    op.create_index("ix_sealed_products_set_id", "sealed_products", ["set_id"])
    op.create_index(
        "ix_sealed_products_upstream_source",
        "sealed_products",
        ["upstream_source"],
    )
    op.create_index(
        "ix_sealed_products_tcg_type",
        "sealed_products",
        ["tcg", "product_type"],
    )
    # Lowercased-name index for case-insensitive search. SQLite ignores
    # the function and just indexes the column, which is still fine.
    op.create_index(
        "ix_sealed_products_name_lower",
        "sealed_products",
        [sa.text("lower(name)")],
    )

    # ── sealed_holdings (ownership) ───────────────────────────────────────
    op.create_table(
        "sealed_holdings",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            UuidCol(),
            sa.ForeignKey("sealed_products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "quantity",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("purchase_price_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("purchase_date", sa.Date(), nullable=True),
        sa.Column("estimated_value_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "acquired_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sealed_holdings_user_id", "sealed_holdings", ["user_id"])
    op.create_index("ix_sealed_holdings_product_id", "sealed_holdings", ["product_id"])
    op.create_index(
        "ix_sealed_holdings_user_acquired",
        "sealed_holdings",
        ["user_id", "acquired_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sealed_holdings_user_acquired", table_name="sealed_holdings")
    op.drop_index("ix_sealed_holdings_product_id", table_name="sealed_holdings")
    op.drop_index("ix_sealed_holdings_user_id", table_name="sealed_holdings")
    op.drop_table("sealed_holdings")

    op.drop_index("ix_sealed_products_name_lower", table_name="sealed_products")
    op.drop_index("ix_sealed_products_tcg_type", table_name="sealed_products")
    op.drop_index("ix_sealed_products_upstream_source", table_name="sealed_products")
    op.drop_index("ix_sealed_products_set_id", table_name="sealed_products")
    op.drop_index("ix_sealed_products_product_type", table_name="sealed_products")
    op.drop_index("ix_sealed_products_tcg", table_name="sealed_products")
    op.drop_table("sealed_products")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(*_TYPE_VALUES, name="sealed_product_type_enum").drop(
            bind, checkfirst=True
        )
