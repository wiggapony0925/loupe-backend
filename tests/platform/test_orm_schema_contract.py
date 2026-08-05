"""DB ↔ ORM ↔ backend schema contract.

Pins every table the backend ORM owns and the critical columns that
grades / collections / membership need, so a missing migration or a
silent model drop fails CI.

Also asserts Alembic has a single head (no branched migration graph).

Note on policies: this stack has **no Postgres RLS**. Authorization is
app-layer (JWT + ``user_id`` filters). See
``test_ownership_policy_contract.py`` for the behavioural stand-in.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect as sa_inspect

from app import models  # noqa: F401 — register every table on Base.metadata
from app.db import Base

BACKEND_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Tables the product relies on. Keep alphabetical; adding a model without
# listing it here fails loudly so the snapshot stays intentional.
# ---------------------------------------------------------------------------
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "ai_search_log",
        "api_keys",
        "application_events",
        "audit_log",
        "blog_posts",
        "card_external_refs",
        "card_identifications",
        "card_sets",
        "cards",
        "catalog_image_hashes",
        "catalog_mirror_cards",
        "catalog_mirror_sets",
        "collection_items",
        "collections",
        "email_log",
        "feature_flags",
        "fingerprints",
        "graded_cards",
        "identification_feedback",
        "job_applications",
        "job_postings",
        "kv_cache",
        "portfolio_snapshots",
        "price_alerts",
        "price_snapshots",
        "pricecharting_prices",
        "push_tokens",
        "scan_jobs",
        "scanners",
        "sealed_holdings",
        "sealed_products",
        "site_config",
        "social_follow_requests",
        "social_follows",
        "social_profiles",
        "social_profile_likes",
        "social_profile_visits",
        "user_recents",
        "user_reports",
        "user_settings",
        "users",
        "waitlist_entries",
        "watchlist_items",
    }
)

# Critical columns for the vault / multi-collection flows landed recently.
# Shape: table → {column: nullable?}
CRITICAL_COLUMNS: dict[str, dict[str, bool]] = {
    "graded_cards": {
        "id": False,
        "user_id": False,
        "card_id": False,
        "grade": False,
        "house": False,
        "condition": True,  # RAW only; slabs store NULL
        "subgrades": True,
        "estimated_value_usd": True,
        "purchase_price_usd": True,
        "purchase_date": True,
        "tags": True,
        "notes": True,
        "acquired_via": True,
        "fingerprint_hash": True,
        "scan_job_id": True,
        "graded_at": False,
        "created_at": False,
        "updated_at": False,
        "deleted_at": True,
    },
    "collections": {
        "id": False,
        "user_id": False,
        "name": False,
        "description": True,
        "color": True,
        "is_public": False,
        "created_at": False,
        "updated_at": False,
    },
    "collection_items": {
        "collection_id": False,
        "graded_card_id": False,
        "added_at": False,
    },
    "user_settings": {
        "user_id": False,
        "active_collection_id": True,
    },
}

# Alembic revisions that introduced the columns above — must stay in the
# linear history forever (guards accidental rewrite / squash without docs).
REQUIRED_REVISIONS: frozenset[str] = frozenset(
    {
        "0001_initial",
        "0004_grade_cost_basis",
        "0007_grade_condition",
        "0023_graded_card_acquired_via",
        "0035_grade_tags",
        "0038_active_collection_pref",
    }
)


def _column_map(table_name: str) -> dict[str, bool]:
    table = Base.metadata.tables[table_name]
    return {c.name: c.nullable for c in table.columns}


def test_orm_exposes_every_expected_table() -> None:
    actual = set(Base.metadata.tables)
    missing = EXPECTED_TABLES - actual
    extra = actual - EXPECTED_TABLES
    assert not missing, f"ORM missing tables: {sorted(missing)}"
    # Extra tables are OK only when intentional — fail so authors update
    # EXPECTED_TABLES when adding a model.
    assert not extra, (
        f"ORM has unexpected tables {sorted(extra)}. "
        "Add them to EXPECTED_TABLES if intentional."
    )


def test_critical_grade_and_collection_columns() -> None:
    for table, expected in CRITICAL_COLUMNS.items():
        assert table in Base.metadata.tables, f"missing table {table}"
        actual = _column_map(table)
        for col, nullable in expected.items():
            assert col in actual, f"{table}.{col} missing from ORM"
            assert actual[col] is nullable, (
                f"{table}.{col} nullable={actual[col]}, expected {nullable}"
            )


def test_collection_items_composite_pk() -> None:
    table = Base.metadata.tables["collection_items"]
    pk = {c.name for c in table.primary_key.columns}
    assert pk == {"collection_id", "graded_card_id"}


def test_graded_cards_fk_and_indexes() -> None:
    table = Base.metadata.tables["graded_cards"]
    targets = set()
    for col in table.columns:
        for fk in col.foreign_keys:
            targets.add((col.name, fk.column.table.name, fk.column.name))
    assert ("user_id", "users", "id") in targets
    assert ("card_id", "cards", "id") in targets
    index_cols = {tuple(ix.columns.keys()) for ix in table.indexes}
    assert any("user_id" in cols and "graded_at" in cols for cols in index_cols)


def test_alembic_single_head() -> None:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    # Point script_location explicitly — alembic.ini uses a relative path.
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "app" / "db" / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"branched alembic heads: {heads}"


def test_alembic_history_includes_grade_collection_revisions() -> None:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "app" / "db" / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    revision_ids = {rev.revision for rev in script.walk_revisions()}
    # Alembic ids are often short hashes OR our explicit 000N names —
    # also match on down_revision chain filenames via module path.
    files = {
        p.stem
        for p in (BACKEND_ROOT / "app" / "db" / "alembic" / "versions").glob("*.py")
        if p.name != "__init__.py"
    }
    missing = REQUIRED_REVISIONS - files
    assert not missing, f"required migration files missing: {sorted(missing)}"
    assert revision_ids, "alembic script directory is empty"


def test_sqlalchemy_inspector_sees_create_all_tables() -> None:
    """In-memory SQLite create_all must materialize the same table set
    pytest uses — catches broken __table_args__ / Enum dialects early."""
    from sqlalchemy import create_engine

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    insp = sa_inspect(eng)
    names = set(insp.get_table_names())
    assert names >= EXPECTED_TABLES, sorted(EXPECTED_TABLES - names)
    cols = {c["name"] for c in insp.get_columns("graded_cards")}
    assert {"grade", "house", "condition", "tags", "purchase_price_usd"} <= cols
    item_cols = {c["name"] for c in insp.get_columns("collection_items")}
    assert {"collection_id", "graded_card_id", "added_at"} <= item_cols
