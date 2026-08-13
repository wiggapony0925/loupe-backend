"""The alembic chain: is it well-formed, and does it still build the schema?

Two different questions live in this file, and they fail for different
reasons, so they are kept apart deliberately.

The first is STRUCTURAL and needs no database: one head, no duplicate
revision ids, every ``down_revision`` pointing at something real, one base,
and an ``upgrade``/``downgrade`` pair in every file. These are the cheapest
tests in the repo and they catch the mistake that actually happens — two
people cutting a migration from the same parent in the same week (which is
exactly how ``0044_notifications`` and ``0044_social`` came to exist).

The second is BEHAVIOURAL and needs postgres: does running the chain from
an empty database produce the schema the models describe? Nothing in this
repo has ever asked that question, because every other test builds its
schema with ``create_all`` against SQLite. The answer today is NO, and
:func:`test_a_fresh_database_can_be_built_by_running_the_migration_chain`
is written as an xfail that says precisely why. See that test for the
detail; the short version is that ``0001_initial`` calls
``Base.metadata.create_all()``, so it does not build the 2024 schema it
claims to — it builds TODAY's — and every later migration then tries to add
something that is already there.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_DIR = BACKEND_ROOT / "app" / "db" / "alembic"
VERSIONS_DIR = ALEMBIC_DIR / "versions"

# Reflected schemas: table -> column -> (rendered type, nullable).
Snapshot = dict[str, dict[str, tuple[str, bool]]]

# Alembic's own bookkeeping table exists only in a database that migrations
# were run against, never in a create_all one — comparing the two would
# always "differ" by it, which says nothing about the schema.
BOOKKEEPING = "alembic_version"


# ---------------------------------------------------------------------------
# Reading the chain off disk. No database, no alembic runtime — deliberately
# independent of the tool, so a test can still tell the truth about the files
# on a day alembic itself refuses to load them.
# ---------------------------------------------------------------------------


def _version_files() -> list[Path]:
    return sorted(p for p in VERSIONS_DIR.glob("*.py") if p.name != "__init__.py")


def _literal(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None


def _module_assignments(path: Path) -> dict[str, object]:
    """Module-level ``name = literal`` pairs, annotated or not."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                found[node.target.id] = _literal(node.value)
        elif isinstance(node, ast.Assign) and node.value is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = _literal(node.value)
    return found


def _module_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _down_revisions(value: object) -> tuple[str, ...]:
    """Normalise ``down_revision`` — None, a string, or a merge tuple."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(v) for v in value)
    raise AssertionError(f"unreadable down_revision: {value!r}")


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(ALEMBIC_INI))
    # alembic.ini's script_location is relative to the repo root; pytest may
    # be invoked from anywhere, so make it absolute.
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    return ScriptDirectory.from_config(cfg)


# ---------------------------------------------------------------------------
# 1. The chain is well-formed. These run with postgres down.
# ---------------------------------------------------------------------------


def test_the_migration_chain_has_exactly_one_head() -> None:
    """Two heads means two people's migrations never met.

    It is survivable in production — the deploy job runs ``upgrade heads``
    and ``alembic_version`` simply carries a row per branch — but it makes
    "what is the schema?" a question with two answers, and the next
    ``upgrade head`` (singular) fails outright. The 0044 fork is the proof
    this happens here; ``0046_merge_heads`` is the repair. The head id is
    not pinned on purpose: adding a migration is normal, branching is not.
    """
    heads = _script_directory().get_heads()
    assert len(heads) == 1, f"branched alembic heads: {sorted(heads)}"


def test_the_migration_chain_goes_back_to_exactly_one_base() -> None:
    """One base = one origin story for the schema.

    A second base is a migration with ``down_revision = None`` that someone
    added by copying a template — it would silently create a whole parallel
    history that ``upgrade head`` may or may not ever apply.
    """
    bases = sorted(_script_directory().get_bases())
    assert bases == ["0001_initial"], f"expected a single base, got {bases}"


def test_no_two_migrations_claim_the_same_revision_id() -> None:
    """Revision ids are the chain's primary key.

    Duplicates are not a naming nit: ``alembic_version`` stores the id, so
    two files sharing one id makes "which migration has been applied?"
    unanswerable and can skip a migration entirely on the next upgrade.
    Note that a shared numeric PREFIX is fine and does exist here
    (``0044_notifications`` / ``0044_social``) — the full id is what counts.
    """
    seen: dict[str, list[str]] = {}
    for path in _version_files():
        revision = _module_assignments(path).get("revision")
        assert isinstance(revision, str) and revision, (
            f"{path.name} does not define a literal `revision` id"
        )
        seen.setdefault(revision, []).append(path.name)
    duplicates = {rev: files for rev, files in seen.items() if len(files) > 1}
    assert not duplicates, f"duplicate revision ids: {duplicates}"


def test_every_down_revision_points_at_a_migration_that_exists() -> None:
    """A dangling parent breaks the chain at a point nobody notices.

    Alembic resolves ``down_revision`` lazily, so a typo (or a deleted
    migration) surfaces as a KeyError in the deploy job rather than in
    review. Checking it from the files means the whole graph is verified,
    including the merge revision's tuple of parents.
    """
    known = {_module_assignments(path).get("revision") for path in _version_files()}
    dangling: dict[str, list[str]] = {}
    for path in _version_files():
        parents = _down_revisions(_module_assignments(path).get("down_revision"))
        missing = [p for p in parents if p not in known]
        if missing:
            dangling[path.name] = missing
    assert not dangling, f"down_revision points at unknown revisions: {dangling}"


def test_every_migration_file_is_named_for_its_revision_id() -> None:
    """The filename is used as the revision id by other tests and by people.

    ``tests/platform/test_orm_schema_contract.py`` checks required
    revisions by globbing filenames, and every code review here refers to
    migrations by file. If a file's stem and its ``revision`` ever drift,
    both stop meaning the same thing.
    """
    mismatched = {
        path.name: _module_assignments(path).get("revision")
        for path in _version_files()
        if _module_assignments(path).get("revision") != path.stem
    }
    assert not mismatched, f"filename / revision id mismatch: {mismatched}"


def test_every_migration_defines_both_upgrade_and_downgrade() -> None:
    """A migration without a downgrade is a one-way door.

    The body may legitimately be ``pass`` (``0046_merge_heads`` is a no-op
    merge), but the function has to exist: ``alembic downgrade`` calls it by
    name, so a missing one turns a rollback into an AttributeError at the
    worst possible moment.
    """
    incomplete = {
        path.name: sorted({"upgrade", "downgrade"} - _module_functions(path))
        for path in _version_files()
        if not {"upgrade", "downgrade"} <= _module_functions(path)
    }
    assert not incomplete, f"migrations missing functions: {incomplete}"


def test_the_walked_chain_covers_every_migration_file() -> None:
    """Every file on disk is reachable from the head.

    A file that alembic never walks is a migration that will never run —
    the work looks done in the diff and is missing in the database.
    """
    walked = {rev.revision for rev in _script_directory().walk_revisions()}
    on_disk = {path.stem for path in _version_files()}
    assert walked == on_disk, (
        f"unreachable from head: {sorted(on_disk - walked)}; "
        f"walked but not on disk: {sorted(walked - on_disk)}"
    )


# ---------------------------------------------------------------------------
# 2. Running the chain for real. Needs postgres; skips without it.
# ---------------------------------------------------------------------------


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the alembic CLI against ``url`` exactly like the deploy job does.

    A subprocess rather than ``alembic.command``: ``env.py`` ends in
    ``asyncio.run(...)``, which cannot be called from inside the running
    event loop of an async test. It also keeps the target database honest —
    the URL is injected through the environment and nothing else in the
    process can redirect it.
    """
    env = os.environ | {"DATABASE_URL": url, "APP_ENV": "test"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _failure_report(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
    interesting = [ln for ln in lines if "Error" in ln or "error" in ln]
    tail = interesting[-3:] or lines[-5:]
    return "\n".join(tail)


@asynccontextmanager
async def _scratch_database(pg_url: str, suffix: str) -> AsyncIterator[str]:
    """A second throwaway database beside the suite's own, dropped after.

    Created FROM the suite's scratch database, so the only server this ever
    talks to is the one the fixtures already chose, and the developer's
    ``loupe`` database is never opened at all.
    """
    head, _, base_name = pg_url.rpartition("/")
    name = f"{base_name}_{suffix}"[:63]
    url = f"{head}/{name}"

    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        # pgvector up front, exactly like the pg_engine fixture: 0037 does
        # install it, but an upgrade that stops before 0037 never gets
        # there, and no comparison here should turn on a missing extension.
        seed = create_async_engine(url, isolation_level="AUTOCOMMIT")
        async with seed.connect() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await seed.dispose()
        yield url
    finally:
        async with admin.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin.dispose()


def _read_schema(sync_conn: Connection) -> Snapshot:
    insp = sa_inspect(sync_conn)
    return {
        table: {
            col["name"]: (str(col["type"]), bool(col["nullable"]))
            for col in insp.get_columns(table)
        }
        for table in insp.get_table_names()
        if table != BOOKKEEPING
    }


async def _schema_of(url: str) -> Snapshot:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_read_schema)
    finally:
        await engine.dispose()


async def _schema_of_engine(engine: AsyncEngine) -> Snapshot:
    async with engine.connect() as conn:
        return await conn.run_sync(_read_schema)


def _describe_differences(models: Snapshot, migrated: Snapshot) -> list[str]:
    """Human-readable schema diff — an assert that names the drift."""
    problems: list[str] = []
    for table in sorted(set(models) - set(migrated)):
        problems.append(f"table {table}: in models, missing from migrations")
    for table in sorted(set(migrated) - set(models)):
        problems.append(f"table {table}: created by migrations, not in models")
    for table in sorted(set(models) & set(migrated)):
        mine, theirs = models[table], migrated[table]
        for col in sorted(set(mine) - set(theirs)):
            problems.append(f"{table}.{col}: in models, missing from migrations")
        for col in sorted(set(theirs) - set(mine)):
            problems.append(f"{table}.{col}: created by migrations, not in models")
        for col in sorted(set(mine) & set(theirs)):
            if mine[col] != theirs[col]:
                problems.append(
                    f"{table}.{col}: models {mine[col]} != migrations {theirs[col]}"
                )
    return problems


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason=(
        "`alembic upgrade head` cannot build an empty database. 0001_initial "
        "does not contain the 2024 schema it documents — it calls "
        "Base.metadata.create_all(), which builds TODAY's models — so the very "
        "next revision, 0002_password_hash, runs ALTER TABLE users ADD COLUMN "
        "password_hash and postgres answers DuplicateColumnError: column "
        '"password_hash" of relation "users" already exists. 48 of the 55 '
        "later revisions fail the same way (the 7 survivors are the ones "
        "written idempotently with IF NOT EXISTS). Because env.py wraps the "
        "whole upgrade in one transaction, the failure rolls back to an empty "
        "database — the chain builds nothing at all. When this XPASSes, the "
        "chain has been repaired: delete this marker."
    ),
)
async def test_a_fresh_database_can_be_built_by_running_the_migration_chain(
    pg_url: str, pg_engine: AsyncEngine
) -> None:
    """The migrations and the models must describe the same schema.

    This is the only test in the repo that asks whether the thing we run in
    production — ``alembic upgrade head`` against a database that does not
    exist yet — actually works. Every other test builds its schema straight
    from the models, so the chain has never been exercised end to end.

    It builds the same schema twice: once via ``Base.metadata.create_all``
    (what the models say) and once via the migration chain (what a new
    environment would get), then compares tables, columns, types and
    nullability. It is expected to fail today; the marker says why.
    """
    async with _scratch_database(pg_url, "chain") as url:
        proc = _alembic(url, "upgrade", "head")
        assert proc.returncode == 0, (
            f"`alembic upgrade head` failed:\n{_failure_report(proc)}"
        )
        migrated = await _schema_of(url)

    models = await _schema_of_engine(pg_engine)
    problems = _describe_differences(models, migrated)
    assert not problems, "models and migration chain disagree:\n" + "\n".join(problems)


@pytest.mark.asyncio
async def test_upgrading_to_0001_initial_alone_builds_the_whole_current_schema(
    pg_url: str, pg_engine: AsyncEngine
) -> None:
    """The workaround the team is actually using, pinned as a contract.

    Since ``0001_initial`` is ``create_all`` in a trench coat, stopping the
    upgrade there is how a working database gets built today — and it does
    reproduce the models' whole schema, today's, not 2024's. That is a real
    property worth protecting: it is the difference between "onboarding is
    one command" and "onboarding is a pg_dump from staging". If someone
    rewrites 0001 into honest 2024 DDL (which is the right fix), this test
    fails and points at the runbook that needs updating at the same time.

    "The models' whole schema" is the careful phrasing: it is not quite the
    production schema. See the next test for the one table it misses.
    """
    async with _scratch_database(pg_url, "one") as url:
        proc = _alembic(url, "upgrade", "0001_initial")
        assert proc.returncode == 0, (
            f"`alembic upgrade 0001_initial` failed:\n{_failure_report(proc)}"
        )
        migrated = await _schema_of(url)

    models = await _schema_of_engine(pg_engine)
    problems = _describe_differences(models, migrated)
    assert not problems, (
        "0001_initial no longer reproduces the models' schema:\n" + "\n".join(problems)
    )


@pytest.mark.asyncio
async def test_the_pgvector_embeddings_table_is_in_every_local_database(
    pg_engine: AsyncEngine,
) -> None:
    """``catalog_card_embeddings`` has a model, so create_all makes it too.

    0037 creates the table (and its ivfflat cosine index) with raw SQL,
    because at the time neither the ``vector`` column type nor an ivfflat
    index had an expression in this repo's models. For as long as that was
    the only definition, two facts multiplied: the table existed only in the
    chain, and the chain cannot be run from scratch — so every database
    built the way this repo builds them (``create_all``, or ``upgrade
    0001_initial``, which is the same thing) had no embeddings table, and
    the matcher was a feature that worked in production and nowhere else.

    ``app/models/catalog_embedding.py`` closes that: the column is a
    ``pgvector.sqlalchemy.Vector`` and the index is declared in
    ``__table_args__``, so the models and 0037 now describe the same table
    and a metadata-built database can exercise the matcher. The columns are
    checked here rather than merely the name, because "a table called
    catalog_card_embeddings exists" is satisfied by an empty shell — it is
    the ``vector`` column that decides whether ``<=>`` can run at all.
    """
    schema = await _schema_of_engine(pg_engine)
    assert "catalog_card_embeddings" in schema, (
        "a create_all database has no embeddings table — the model in "
        "app/models/catalog_embedding.py is not registered on Base.metadata"
    )
    columns = schema["catalog_card_embeddings"]
    assert set(columns) == {"card_id", "embedding", "model", "updated_at"}
    assert columns["embedding"] == ("VECTOR(512)", False), (
        f"the embedding column is not a non-null pgvector column: "
        f"{columns['embedding']}"
    )
    # The mirror table it hangs off comes from the models as well — the FK
    # between them is why the embedding rows die with the cards they describe.
    assert "catalog_mirror_cards" in schema


@pytest.mark.asyncio
async def test_upgrading_to_0001_initial_records_that_revision_and_no_other(
    pg_url: str,
) -> None:
    """``alembic_version`` is the database's memory of where it is.

    A partial upgrade must leave exactly the revision it stopped at, so the
    next ``upgrade`` resumes rather than replays. Worth its own test because
    the previous one would still pass if the version table were empty.
    """
    async with _scratch_database(pg_url, "onever") as url:
        proc = _alembic(url, "upgrade", "0001_initial")
        assert proc.returncode == 0, _failure_report(proc)

        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                rows = (
                    (await conn.execute(text(f"SELECT version_num FROM {BOOKKEEPING}")))
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

    assert rows == ["0001_initial"]


@pytest.mark.asyncio
async def test_stamping_head_leaves_exactly_one_row_in_alembic_version(
    pg_url: str,
) -> None:
    """One head must stamp as one row — anything else is a branched history.

    ``alembic stamp head`` is what you run after building a database out of
    band (which, given the chain is broken, is what everyone here does). If
    the graph ever branches again, this records TWO rows, and the next
    person to run ``upgrade head`` gets an error instead of a deploy. So
    this is the single-head invariant asserted where it actually bites: in
    the database, not in the files.
    """
    expected = _script_directory().get_heads()

    async with _scratch_database(pg_url, "stamp") as url:
        proc = _alembic(url, "stamp", "head")
        assert proc.returncode == 0, _failure_report(proc)

        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                rows = (
                    (await conn.execute(text(f"SELECT version_num FROM {BOOKKEEPING}")))
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

    assert len(rows) == 1, f"alembic_version holds {rows}, expected one row"
    assert list(rows) == list(expected)


@pytest.mark.asyncio
async def test_a_stamped_database_is_only_bookkeeping_and_no_tables(
    pg_url: str,
) -> None:
    """Stamping claims history; it does not create anything.

    Stated because the workaround above relies on it: ``stamp`` after a
    ``create_all`` is only safe if stamping itself never touches the schema.
    """
    async with _scratch_database(pg_url, "bare") as url:
        proc = _alembic(url, "stamp", "head")
        assert proc.returncode == 0, _failure_report(proc)
        schema = await _schema_of(url)

    assert schema == {}, f"stamp created tables: {sorted(schema)}"


@pytest.mark.asyncio
async def test_the_broken_chain_leaves_no_half_built_database_behind(
    pg_url: str,
) -> None:
    """When the upgrade fails, it fails cleanly — and that is why it is safe.

    ``env.py`` wraps the whole run in a single transaction rather than one
    per migration, so the known break aborts everything: no tables, no
    ``alembic_version``, nothing to clean up by hand. Documented as a test
    because it is the reason the broken chain is an onboarding annoyance
    instead of an incident, and because switching env.py to
    ``transaction_per_migration`` would quietly change that.

    This test asserts ACTUAL behaviour, not desired behaviour: it passes
    while ``upgrade head`` is broken, and the assertion is written so that a
    repaired chain (returncode 0) satisfies it too.
    """
    async with _scratch_database(pg_url, "abort") as url:
        proc = _alembic(url, "upgrade", "head")
        if proc.returncode == 0:
            pytest.skip("the migration chain now upgrades cleanly — nothing to abort")
        assert (
            "DuplicateColumnError" in proc.stderr or "already exists" in proc.stderr
        ), f"upgrade head failed for an unexpected reason:\n{_failure_report(proc)}"
        schema = await _schema_of(url)

    assert schema == {}, f"a failed `upgrade head` left tables behind: {sorted(schema)}"


@pytest.mark.asyncio
async def test_running_migrations_never_touches_the_developers_database(
    pg_url: str,
) -> None:
    """The guard rail for everything above.

    These tests shell out to alembic, and alembic's env.py reads
    ``DATABASE_URL`` from the environment — which the developer's shell and
    ``.env`` both like to set to the real ``loupe`` database. If injection
    ever stopped working, the tests in this file would silently migrate the
    database someone is working in. So: ask alembic itself which URL it
    resolved, and require it to be the scratch one.
    """
    async with _scratch_database(pg_url, "guard") as url:
        proc = _alembic(url, "current", "--verbose")
        assert proc.returncode == 0, _failure_report(proc)
        scratch_name = url.rpartition("/")[2]
        assert scratch_name in proc.stdout, (
            "alembic did not resolve the injected DATABASE_URL:\n" + proc.stdout
        )
