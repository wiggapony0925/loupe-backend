"""Schema parity — is the database postgres builds the one the models declare?

WHY THIS FILE EXISTS. Every other test in the repo builds its schema in
SQLite, where a VARCHAR(255) holds a megabyte, a UUID is text, a
``TIMESTAMP WITH TIME ZONE`` is a string, an ENUM is nothing at all, and
``ON DELETE CASCADE`` is parsed and ignored unless foreign keys are
switched on. A model can therefore declare a rule that SQLite silently
does not implement, and 2,089 green tests will never notice. The rules in
this file are the ones postgres actually enforces in production, so this
is the first place they are read back off a real server and compared,
column by column, with what ``Base.metadata`` says they should be.

WHAT IT CAN AND CANNOT PROVE. The scratch database is built by
``create_all`` from the same metadata, so a match here does not prove the
*deployed* database is right — that is the migration chain's job. What it
does prove is that the DDL SQLAlchemy renders **on the postgres dialect**
carries the properties the models intend: lengths, nullability, native
enum types, timezone-awareness, real FK constraints with the right
``ON DELETE`` action, and real uniqueness. Those are all things that
change per dialect, and no other test in the repo has ever seen them.
It also pins the postgres schema against silent drift: the day a model
grows a column, loses a cascade, or has its varchar shortened, the
comparison names the column.

A note on the fixtures: these tests take ``pg_engine`` and open their own
connection rather than ``pg_session``, because nothing here writes a row —
the whole file is one read of the schema, asked many questions.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

import app.models  # noqa: F401 — imported for its side effect: fills Base.metadata
from app.db import Base

PG = postgresql.dialect()

#: Tables that legitimately live in a postgres database without a model
#: behind them. ``alembic_version`` is alembic's bookkeeping; the scratch
#: database is built by ``create_all`` so it will not be here today, but
#: excluding it keeps the "no stray tables" test honest if the harness
#: ever switches to running migrations.
NOT_OURS = {"alembic_version"}

#: Renderings that are the same postgres type spelled two ways. Postgres
#: resolves a bare ``FLOAT`` to ``double precision``, which is what comes
#: back on reflection; treating that as drift would be noise.
SAME_TYPE = {"FLOAT": "DOUBLE PRECISION"}


def _pg_ddl(type_: sa.types.TypeEngine) -> str:
    """The postgres DDL spelling of a type, normalised for comparison."""
    rendered = type_.compile(dialect=PG).upper()
    return SAME_TYPE.get(rendered, rendered)


def _reflect(conn: Connection) -> dict:
    """Read the live schema off the server into a plain snapshot."""
    insp = sa.inspect(conn)
    snapshot: dict = {
        "tables": set(insp.get_table_names()) - NOT_OURS,
        "columns": {},
        "pk": {},
        "fks": {},
        "unique": {},
        "enums": {e["name"]: set(e["labels"]) for e in insp.get_enums()},
    }
    for name in snapshot["tables"]:
        snapshot["columns"][name] = {c["name"]: c for c in insp.get_columns(name)}
        snapshot["pk"][name] = set(
            insp.get_pk_constraint(name)["constrained_columns"] or []
        )
        snapshot["fks"][name] = insp.get_foreign_keys(name)
        # Postgres enforces uniqueness through a unique index either way;
        # SQLAlchemy renders `unique=True` as a UNIQUE constraint and
        # `unique=True, index=True` as a unique index, so both count.
        uniq = {tuple(u["column_names"]) for u in insp.get_unique_constraints(name)}
        uniq |= {
            tuple(i["column_names"])
            for i in insp.get_indexes(name)
            if i["unique"] and all(i["column_names"])
        }
        snapshot["unique"][name] = uniq
    return snapshot


#: The reflected schema, cached for the session. Reflection is read-only
#: and the scratch database is built once and never altered, so paying for
#: 57 tables of ``information_schema`` queries in every test would buy
#: nothing. The fixture stays function-scoped because ``pg_engine`` is.
_SNAPSHOT: dict | None = None


@pytest_asyncio.fixture
async def schema(pg_engine: AsyncEngine) -> dict:
    """The live postgres schema, read off the server."""
    global _SNAPSHOT
    if _SNAPSHOT is None:
        async with pg_engine.connect() as conn:
            _SNAPSHOT = await conn.run_sync(_reflect)
    return _SNAPSHOT


def _model_tables() -> dict[str, sa.Table]:
    return dict(Base.metadata.tables)


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_builds_every_table_the_models_declare_and_no_others(
    schema: dict,
) -> None:
    """A model with no table is a 500; a table with no model is a landmine.

    The second half matters as much as the first: an orphan table is one
    nothing owns, nothing migrates and nothing backs up on purpose, and it
    is how a dropped feature keeps a copy of user data around for years.
    """
    declared = set(_model_tables())
    built = schema["tables"]

    assert declared, "no models were imported — this comparison would be vacuous"
    assert sorted(declared - built) == [], (
        "tables the models declare but postgres lacks"
    )
    assert sorted(built - declared) == [], "tables in postgres that no model declares"


# --------------------------------------------------------------------------
# Columns: names, nullability, types
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_table_has_exactly_the_columns_its_model_declares(
    schema: dict,
) -> None:
    """Named, not counted — a swapped pair of columns has the right count."""
    drift: list[str] = []
    checked = 0
    for name, table in _model_tables().items():
        declared = {c.name for c in table.columns}
        built = set(schema["columns"].get(name, {}))
        checked += len(declared)
        for missing in sorted(declared - built):
            drift.append(f"{name}.{missing} declared by the model, absent in postgres")
        for extra in sorted(built - declared):
            drift.append(f"{name}.{extra} exists in postgres, no model declares it")

    assert checked > 0, "reflected no columns at all"
    assert drift == []


@pytest.mark.asyncio
async def test_a_column_is_nullable_in_postgres_exactly_when_the_model_says_it_is(
    schema: dict,
) -> None:
    """NOT NULL is the only thing standing between us and half-written rows.

    SQLite does enforce NOT NULL, so this is the one column property the
    old suite could have caught — but only for columns some test happens
    to insert. This checks all of them, in both directions: a column the
    model made optional that postgres made mandatory breaks every writer.
    """
    drift: list[str] = []
    checked = 0
    for name, table in _model_tables().items():
        built = schema["columns"].get(name, {})
        for col in table.columns:
            reflected = built.get(col.name)
            if reflected is None:
                continue  # reported by the column-set test
            checked += 1
            if bool(col.nullable) != bool(reflected["nullable"]):
                drift.append(
                    f"{name}.{col.name}: model nullable={col.nullable}, "
                    f"postgres nullable={reflected['nullable']}"
                )

    assert checked > 0, "reflected no columns at all"
    assert drift == []


@pytest.mark.asyncio
async def test_every_column_has_the_postgres_type_and_length_its_model_asked_for(
    schema: dict,
) -> None:
    """Length is part of the type, and a short one truncates silently.

    SQLite ignores ``VARCHAR(n)`` entirely, so a column the model says is
    ``String(320)`` could be ``varchar(120)`` on the server and every test
    in the repo would still pass — right up to the first user with a long
    email address getting a 500 on sign-up. Comparing the compiled
    postgres DDL on both sides checks the family (uuid, numeric, enum,
    timestamptz, json) *and* the declared length/precision at once.
    """
    drift: list[str] = []
    checked = 0
    for name, table in _model_tables().items():
        built = schema["columns"].get(name, {})
        for col in table.columns:
            reflected = built.get(col.name)
            if reflected is None:
                continue
            checked += 1
            want = _pg_ddl(col.type)
            got = _pg_ddl(reflected["type"])
            if want != got:
                drift.append(
                    f"{name}.{col.name}: model wants {want}, postgres has {got}"
                )

    assert checked > 0, "reflected no columns at all"
    assert drift == []


@pytest.mark.asyncio
async def test_no_timestamp_column_is_stored_without_a_timezone(schema: dict) -> None:
    """`timestamp` and `timestamptz` are different bugs waiting to happen.

    Everything in this codebase works in aware UTC datetimes. A column
    reflected as naive ``timestamp`` would accept an aware value, drop the
    offset, and hand back a naive one — which then compares unequal to,
    and cannot be subtracted from, every other datetime in the process.
    SQLite stores all of them as strings and never shows the difference,
    so this rule has never been checked before. It is asserted against the
    server directly rather than against the models, so a new column that
    forgets ``timezone=True`` fails here even though it matches its model.
    """
    naive: list[str] = []
    checked = 0
    for name, columns in schema["columns"].items():
        for col_name, col in columns.items():
            if not isinstance(col["type"], sa.DateTime):
                continue
            checked += 1
            if not col["type"].timezone:
                naive.append(f"{name}.{col_name}")

    assert checked > 0, "found no timestamp columns — reflection is not working"
    assert naive == [], "timestamp columns with no timezone"


@pytest.mark.asyncio
async def test_enum_columns_are_native_postgres_types_carrying_every_python_label(
    schema: dict,
) -> None:
    """A label Python knows and postgres does not is a write that fails.

    These columns compile to real ``CREATE TYPE ... AS ENUM`` types, which
    means postgres — unlike SQLite, which stores them as bare text —
    rejects any value outside the label list. That is the protection we
    want, and it is also the trap: adding a member to a Python enum
    without migrating the postgres type turns the first row that uses it
    into an InvalidTextRepresentation error at runtime.
    """
    drift: list[str] = []
    checked = 0
    for name, table in _model_tables().items():
        for col in table.columns:
            if not isinstance(col.type, sa.Enum):
                continue
            checked += 1
            type_name = col.type.name
            labels = schema["enums"].get(type_name)
            if labels is None:
                drift.append(
                    f"{name}.{col.name}: postgres has no enum type {type_name!r}"
                )
                continue
            declared = set(col.type.enums)
            if declared != labels:
                drift.append(
                    f"{name}.{col.name} ({type_name}): python-only="
                    f"{sorted(declared - labels)}, postgres-only={sorted(labels - declared)}"
                )

    assert checked > 0, "found no enum columns — reflection is not working"
    assert drift == []


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_key_columns_match_the_models_table_for_table(
    schema: dict,
) -> None:
    """The primary key is the identity and the uniqueness guarantee.

    Composite keys are where this bites: ``social_follows`` is keyed on
    (follower, followee), and a key that lost half of itself in the
    database would let the same person follow the same person twice.
    """
    drift: list[str] = []
    for name, table in _model_tables().items():
        declared = {c.name for c in table.primary_key.columns}
        built = schema["pk"].get(name, set())
        if declared != built:
            drift.append(
                f"{name}: model PK {sorted(declared)}, postgres {sorted(built)}"
            )

    assert drift == []


# --------------------------------------------------------------------------
# Foreign keys
# --------------------------------------------------------------------------


def _find_fk(reflected: list[dict], column: str, referred_table: str) -> dict | None:
    for fk in reflected:
        if (
            fk["referred_table"] == referred_table
            and column in fk["constrained_columns"]
        ):
            return fk
    return None


@pytest.mark.asyncio
async def test_every_model_foreign_key_is_a_real_constraint_on_the_right_column(
    schema: dict,
) -> None:
    """A ForeignKey the database does not know about is only a comment.

    SQLite does not enforce foreign keys at all unless
    ``PRAGMA foreign_keys=ON`` is set per connection, so nothing in the
    old suite proves referential integrity exists. Here we check that
    every relationship the models draw is a constraint postgres will
    actually refuse to violate, pointing at the right table *and* the
    right column — a FK aimed at the wrong column of the right table is
    both valid DDL and completely wrong.
    """
    drift: list[str] = []
    checked = 0
    for name, table in _model_tables().items():
        reflected = schema["fks"].get(name, [])
        for fk in table.foreign_keys:
            checked += 1
            column = fk.parent.name
            target_table = fk.column.table.name
            target_column = fk.column.name
            found = _find_fk(reflected, column, target_table)
            if found is None:
                drift.append(
                    f"{name}.{column} -> {target_table}.{target_column}: "
                    "no such constraint in postgres"
                )
                continue
            if found["referred_columns"] != [target_column]:
                drift.append(
                    f"{name}.{column}: model points at {target_table}.{target_column}, "
                    f"postgres points at {target_table}.{found['referred_columns']}"
                )

    assert checked > 0, "the models declare no foreign keys — that cannot be right"
    assert drift == []


@pytest.mark.asyncio
async def test_every_foreign_key_carries_the_on_delete_action_its_model_declares(
    schema: dict,
) -> None:
    """ON DELETE is a data rule, and the database is the only one enforcing it.

    Each of the three actions the models use is load-bearing and means
    something different: CASCADE says this row is worthless without its
    parent (a user's collections), SET NULL says it outlives the parent
    with the link forgotten (an audit log after the actor is deleted), and
    RESTRICT says the parent may not leave while this exists (a catalog
    product someone still holds). A CASCADE the model intends but the
    database lacks does not fail loudly — the delete is simply refused, or
    worse, an ORM-level cascade papers over it until someone deletes a row
    with raw SQL and leaves orphans behind.
    """
    drift: list[str] = []
    checked = 0
    for name, table in _model_tables().items():
        reflected = schema["fks"].get(name, [])
        for fk in table.foreign_keys:
            found = _find_fk(reflected, fk.parent.name, fk.column.table.name)
            if found is None:
                continue  # reported by the constraint-existence test
            checked += 1
            # Postgres omits the default action on reflection; the models
            # spell out every one, so both sides normalise to NO ACTION.
            declared = (fk.ondelete or "NO ACTION").upper()
            built = (
                (found.get("options") or {}).get("ondelete") or "NO ACTION"
            ).upper()
            if declared != built:
                drift.append(
                    f"{name}.{fk.parent.name} -> {fk.column.table.name}: "
                    f"model says ON DELETE {declared}, postgres has {built}"
                )

    assert checked > 0, "compared no foreign keys"
    assert drift == []


# --------------------------------------------------------------------------
# Uniqueness
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_unique_rule_in_the_models_is_enforced_by_postgres(
    schema: dict,
) -> None:
    """Uniqueness in the model is a wish; uniqueness in the index is a fact.

    Application-level "check then insert" cannot be safe under concurrency
    — two requests both check, both find nothing, both insert. The unique
    index is what makes the second one lose. These rules are the ones
    protecting identity (one account per email, per phone, per Apple
    subject) and the ones protecting counts (one like per person per
    post), so a missing one is a duplicate-account or inflated-count bug.
    """
    missing: list[str] = []
    checked = 0
    for name, table in _model_tables().items():
        built = schema["unique"].get(name, set())
        rules: list[tuple[str, tuple[str, ...]]] = [
            ("UniqueConstraint", tuple(c.name for c in constraint.columns))
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        ]
        rules += [
            ("unique Index", tuple(c.name for c in index.columns))
            for index in table.indexes
            if index.unique
        ]
        for kind, columns in rules:
            checked += 1
            if columns not in built:
                missing.append(
                    f"{name} {list(columns)} ({kind}) is not unique in postgres"
                )

    assert checked > 0, "the models declare no unique rules — that cannot be right"
    assert missing == []


# --------------------------------------------------------------------------
# JSON storage, and one place where postgres reality differs from the code
# --------------------------------------------------------------------------


#: The JSON columns that still declare ``sqlalchemy.JSON`` inline instead
#: of ``app.db.types.JsonCol``, and are therefore still ``json`` on the
#: server after ``0057_json_to_jsonb``. Pinned as an exact set rather than
#: tolerated as a wildcard so the list can only shrink: a new column that
#: reaches for a bare ``JSON`` import fails this test the day it is added,
#: and removing the last of them is a one-line edit here.
STILL_PLAIN_JSON = {
    "ai_search_log.candidates",
    "ai_search_log.results",
    "email_log.headers",
    "notifications.data",
    "users.mfa_backup_codes",
}


@pytest.mark.asyncio
async def test_a_json_column_is_jsonb_in_postgres_exactly_when_its_model_says_so(
    schema: dict,
) -> None:
    """``JsonCol`` is jsonb; a bare ``sqlalchemy.JSON`` is json. Both, exactly.

    THE RULE. ``JsonCol`` is ``JSON().with_variant(JSONB(), "postgresql")``,
    so every column declared through it is ``jsonb`` on the server and plain
    JSON on SQLite. The five columns listed in ``STILL_PLAIN_JSON`` import
    ``sqlalchemy.JSON`` directly, which compiles to ``json`` on postgres —
    they are the exception, and this test names them so the exception cannot
    grow quietly.

    WHY THE DISTINCTION IS WORTH A TEST. ``json`` keeps the document as the
    text it arrived as: every read reparses it, the containment operators
    (``@>``, ``?``, ``@@``) are not defined for it, and it cannot carry a GIN
    index — so a filter over ``cards.metadata`` would be a sequential scan
    with a per-row cast, or a syntax error. ``jsonb`` is what makes those
    queries possible, and the difference is invisible everywhere else in the
    suite because SQLite stores both as TEXT.

    This checks both directions on purpose. A column that claims JSONB and
    is json is the old bug coming back. A column that is jsonb while its
    model says ``JSON`` is the mirror-image bug, and a worse one: the
    migration chain and ``Base.metadata`` would have drifted apart, so
    production and every ``create_all`` database would disagree about a type
    with nothing to catch it.
    """
    drift: list[str] = []
    plain: set[str] = set()
    checked = 0
    for name, table in _model_tables().items():
        built = schema["columns"].get(name, {})
        for col in table.columns:
            if not isinstance(col.type, sa.JSON):
                continue
            reflected = built.get(col.name)
            if reflected is None:
                continue  # reported by the column-set test
            checked += 1
            qualified = f"{name}.{col.name}"
            # The model side is asked the same way the DDL is generated:
            # compile the type on the postgres dialect. A variant answers
            # JSONB there and JSON on SQLite, which is the whole point.
            wants_jsonb = _pg_ddl(col.type) == "JSONB"
            is_jsonb = isinstance(reflected["type"], postgresql.JSONB)
            if wants_jsonb != is_jsonb:
                drift.append(
                    f"{qualified}: model wants {'jsonb' if wants_jsonb else 'json'}, "
                    f"postgres has {'jsonb' if is_jsonb else 'json'}"
                )
            if not wants_jsonb:
                plain.add(qualified)

    assert checked > 0, "found no JSON columns — reflection is not working"
    assert drift == []
    assert plain == STILL_PLAIN_JSON, (
        "the set of columns declaring a bare `sqlalchemy.JSON` changed — use "
        "`app.db.types.JsonCol` for new JSON columns, or update this set (and "
        "migrate the column) if one was deliberately converted"
    )


@pytest.mark.asyncio
async def test_the_embedding_table_the_matcher_queries_is_declared_by_a_model(
    schema: dict, pg_engine: AsyncEngine
) -> None:
    """The models ARE the whole schema — pgvector table and ivfflat index too.

    ``catalog_card_embeddings`` used to be created only by raw SQL in
    migration 0037, because nothing here could spell a ``vector(N)`` column
    or an ivfflat index. A table outside ``Base.metadata`` is a table that
    does not exist in any database this repo builds — the SQLite suite, and
    the ``alembic upgrade 0001_initial`` bootstrap (0001 is ``create_all``)
    — so the learned-embedding matcher could only ever work in a long-lived
    environment that had been migrated revision by revision.

    ``app/models/catalog_embedding.py`` now declares it with
    ``pgvector.sqlalchemy.Vector`` and an ``__table_args__`` index carrying
    ``postgresql_using="ivfflat"`` and the ``vector_cosine_ops`` opclass, so
    both halves are checked here: the model is registered, and postgres
    really built the table AND an index the planner can use. The index is
    the half that would fail silently — a plain btree on a vector column is
    valid DDL and useless for ``<=>``, and the query would fall back to a
    sequential scan that gets slower with every card in the catalog.
    """
    assert "catalog_card_embeddings" in Base.metadata.tables, (
        "no model declares the table the embedding matcher queries"
    )
    assert "catalog_card_embeddings" in schema["tables"], (
        "create_all did not build catalog_card_embeddings — the pgvector "
        "extension or the model's before_create hook is missing"
    )

    def _indexes(conn: Connection) -> list[dict]:
        return sa.inspect(conn).get_indexes("catalog_card_embeddings")

    async with pg_engine.connect() as conn:
        indexes = await conn.run_sync(_indexes)

    cosine = next(
        (i for i in indexes if i["name"] == "ix_card_embeddings_cosine"), None
    )
    assert cosine is not None, (
        f"ix_card_embeddings_cosine is not on the table; found {indexes}"
    )
    assert cosine["column_names"] == ["embedding"]
    options = cosine.get("dialect_options") or {}
    assert options.get("postgresql_using") == "ivfflat", (
        f"the cosine index is not an ivfflat index: {options}"
    )
    assert options.get("postgresql_ops") == {"embedding": "vector_cosine_ops"}, (
        f"the ivfflat index is not built for cosine distance: {options}"
    )
