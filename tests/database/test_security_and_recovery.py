"""Security boundaries and backup/recovery — the non-functional guarantees.

WHY THIS FILE EXISTS. The rest of the suite asks "does the code do the
right thing?". This file asks the two questions that only matter on the
day something goes wrong: *what can an attacker who reaches the database
do*, and *can we get the data back*. Neither has ever been asserted here,
because SQLite has no roles, no grants, no read-only transactions, and no
pg_dump.

Two of these tests document a posture rather than enforce one: the
application connects as a SUPERUSER today, and saying so in a test is
worth more than a test that pretends otherwise. The rest prove the
mechanisms the team would use to fix that actually work on this server,
so adopting them is a config change and not a research project.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import delete, make_url, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.models.user import User, UserSettings
from tests.factories import make_user

REPO_ROOT = Path(__file__).resolve().parents[2]


def _sqlstate(exc: BaseException) -> str | None:
    """The five-character SQLSTATE asyncpg attached to a wrapped error.

    Asserting on the code rather than the message is deliberate: messages
    are localised and get reworded between postgres releases, SQLSTATEs
    are part of the wire contract.
    """
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


# ──────────────────────────────────────────────────────────────────────
# Security: what the application role can actually do
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_application_role_is_a_superuser_that_owns_every_table(pg_engine):
    """Document the true privilege posture: there is no least privilege.

    The app connects with the credentials in DATABASE_URL — the same
    ``loupe`` role compose creates as POSTGRES_USER — and that role is a
    cluster SUPERUSER which owns every table. Concretely that means one
    SQL injection bug is not "an attacker read a table"; it is DROP
    TABLE, TRUNCATE, ALTER, reading every other database on the server,
    bypassing any RLS policy we ever add, and COPY ... FROM PROGRAM,
    which is arbitrary command execution as the postgres unix user.

    A hardened setup separates three roles: a migration/owner role that
    owns the schema and is used only by alembic, an application role with
    SELECT/INSERT/UPDATE/DELETE on the tables it needs and nothing else,
    and a read-only role for analytics and the loupe.data query pane.
    Nobody in that picture is a superuser and nobody but the migrator can
    run DDL.

    This test asserts today's reality on purpose. If it starts failing
    because ``rolsuper`` is false, least privilege has landed — that is a
    win, and the assertions below should be rewritten to pin the new,
    narrower grants rather than deleted.
    """
    async with pg_engine.connect() as conn:
        role = (
            await conn.execute(
                text(
                    "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, "
                    "rolbypassrls, rolcanlogin "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()

        # Ownership is not a grant you can see in information_schema — it is
        # what confers DROP and ALTER, so it has to be read off pg_tables.
        owners = (
            (
                await conn.execute(
                    text(
                        "SELECT DISTINCT tableowner FROM pg_tables "
                        "WHERE schemaname = 'public'"
                    )
                )
            )
            .scalars()
            .all()
        )

        grants = (
            (
                await conn.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_schema = 'public' AND table_name = 'users' "
                        "AND grantee = current_user"
                    )
                )
            )
            .scalars()
            .all()
        )

        can_create_in_public = (
            await conn.execute(
                text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            )
        ).scalar_one()

    assert role.rolsuper is True, (
        "the application role is no longer a superuser — if that was "
        "deliberate, update this test to pin the new grants"
    )
    assert role.rolcanlogin is True
    # Superuser implies all of these; they are spelled out so a future
    # "we dropped superuser" change has to confront each one it kept.
    assert (role.rolcreatedb, role.rolcreaterole, role.rolbypassrls) == (
        True,
        True,
        True,
    )

    assert owners == [role.rolname], (
        f"expected every public table to be owned by {role.rolname!r}, got {owners}"
    )
    # The full owner ACL: everything postgres can grant on a table.
    assert set(grants) == {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    }
    assert can_create_in_public is True


@pytest.mark.asyncio
async def test_the_application_role_can_drop_the_table_it_reads(pg_session):
    """The blast radius of one injection bug, demonstrated rather than argued.

    Owning a table *is* the privilege to destroy it; there is no separate
    "DROP" grant to revoke. So this drops ``users`` for real inside a
    savepoint and rolls it straight back. If the app role were the
    non-owner it should be, the DROP would raise 42501 instead — which is
    exactly what ``test_a_select_only_role_is_refused_every_write``
    proves is achievable on this server.
    """
    savepoint = await pg_session.begin_nested()
    await pg_session.execute(text("DROP TABLE user_settings, users CASCADE"))
    gone = (
        await pg_session.execute(text("SELECT to_regclass('public.users')"))
    ).scalar_one()
    assert gone is None, "DROP TABLE was refused — the role is no longer the owner"

    # DDL is transactional in postgres, so the rollback puts it back and no
    # other test in this session ever sees the table missing.
    await savepoint.rollback()
    back = (
        await pg_session.execute(text("SELECT to_regclass('public.users')"))
    ).scalar_one()
    assert back == "users"


@pytest.mark.asyncio
async def test_a_select_only_role_is_refused_every_write(pg_engine, pg_url):
    """Prove the boundary the app does not use today actually holds.

    Creates a real login role granted SELECT on exactly one table,
    connects as it, and checks postgres refuses INSERT, UPDATE, DELETE,
    DROP, CREATE TABLE, and even SELECT on a table it was not granted.
    Every refusal is 42501 (insufficient_privilege) — enforced by the
    server, so it cannot be reasoned around by application code.

    This is the evidence that the hardened setup described in
    ``test_the_application_role_is_a_superuser_that_owns_every_table`` is
    a few GRANT statements away, not a rewrite.
    """
    url = make_url(pg_url)
    role = f"loupe_ro_{uuid.uuid4().hex[:10]}"
    # Hex only, and inlined because utility statements (CREATE ROLE) take no
    # bind parameters — there is nothing here that could close a quote.
    password = uuid.uuid4().hex
    database = url.database

    restricted: AsyncEngine | None = None
    try:
        async with pg_engine.connect() as conn:
            # AUTOCOMMIT: roles are cluster-wide and a second connection has
            # to be able to see this one before it can authenticate as it.
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(
                text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
            )
            await conn.execute(
                text(f'GRANT CONNECT ON DATABASE "{database}" TO "{role}"')
            )
            await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            await conn.execute(text(f'GRANT SELECT ON TABLE users TO "{role}"'))

        restricted = create_async_engine(url.set(username=role, password=password))

        async with restricted.connect() as conn:
            who = (await conn.execute(text("SELECT current_user"))).scalar_one()
            assert who == role
            # The one thing it may do.
            await conn.execute(text("SELECT count(*) FROM users"))

        refused = [
            # Spelled out to every NOT NULL column on purpose: this INSERT
            # would SUCCEED for the app role, so the refusal below can only
            # be about privileges and not about a malformed row.
            (
                "INSERT INTO users (id, email, is_admin, failed_login_count, "
                "token_version, mfa_enabled, plan, pro_trialing) VALUES "
                "(gen_random_uuid(), 'x@example.com', false, 0, 0, false, 'free', false)"
            ),
            "UPDATE users SET display_name = 'x'",
            "DELETE FROM users",
            "DROP TABLE users",
            "CREATE TABLE not_allowed (id int)",
            # The grant was per-table: a neighbouring table stays invisible.
            "SELECT * FROM card_sets",
        ]
        for sql in refused:
            # A failed statement poisons its transaction, so each gets a
            # connection of its own.
            async with restricted.connect() as conn:
                with pytest.raises(DBAPIError) as err:
                    await conn.execute(text(sql))
            assert _sqlstate(err.value) == "42501", (
                f"expected insufficient_privilege for {sql!r}, "
                f"got {_sqlstate(err.value)}"
            )
    finally:
        if restricted is not None:
            await restricted.dispose()
        async with pg_engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            # Roles outlive the scratch database, so this must not leak one
            # into the developer's cluster. DROP OWNED BY clears the grants
            # (including the shared database-level one) that would otherwise
            # make DROP ROLE fail.
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))


@pytest.mark.asyncio
async def test_a_drop_table_payload_bound_as_a_parameter_stays_text(pg_session):
    """Bound parameters are the whole injection defence — prove they bind.

    The payload below is the canonical one. Sent as a bind parameter it
    never reaches the parser as SQL: postgres receives it in the Bind
    message of the extended query protocol, as a value. So the table
    survives, the string round-trips byte for byte, and it can even be
    used as a search term. The moment someone f-strings user input into
    a query instead, this stops being true — which is why the assertion
    is about ``users`` still existing and not just about the value.
    """
    payload = "Robert'); DROP TABLE users; --"

    user = await make_user(pg_session)
    user.display_name = payload
    await pg_session.flush()

    # Same payload through raw SQL with a bind parameter, the path a hand
    # written query would take.
    await pg_session.execute(
        text("UPDATE users SET avatar_url = :v WHERE id = :id"),
        {"v": payload, "id": user.id},
    )

    still_there = (
        await pg_session.execute(text("SELECT to_regclass('public.users')"))
    ).scalar_one()
    assert still_there == "users", "the payload executed — parameters were not bound"

    stored = (
        await pg_session.execute(
            text("SELECT display_name, avatar_url FROM users WHERE id = :id"),
            {"id": user.id},
        )
    ).one()
    assert stored.display_name == payload
    assert stored.avatar_url == payload

    # And it is ordinary data: it compares as a value, not as syntax.
    matched = (
        await pg_session.execute(
            text("SELECT count(*) FROM users WHERE display_name = :v"),
            {"v": payload},
        )
    ).scalar_one()
    assert matched == 1


@pytest.mark.asyncio
async def test_a_read_only_transaction_refuses_every_write_with_25006(pg_engine):
    """``BEGIN TRANSACTION READ ONLY`` is a server-side guarantee, not a promise.

    This is what an ad-hoc query pane (loupe.data) should wrap every
    user-supplied statement in: whatever SQL arrives, the transaction
    cannot write. Postgres refuses INSERT/UPDATE/DELETE and DDL with
    25006 (read_only_sql_transaction) — no allowlist parsing of the
    incoming SQL required, which is the only version of this that is
    actually safe.
    """
    async with pg_engine.connect() as conn:
        # AUTOCOMMIT so the BEGIN below is ours and reads literally, exactly
        # as the query pane would send it.
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("BEGIN TRANSACTION READ ONLY"))
        try:
            mode = (await conn.execute(text("SHOW transaction_read_only"))).scalar_one()
            assert mode == "on"

            # Reads are unaffected — the pane still has to be useful.
            await conn.execute(text("SELECT count(*) FROM users"))

            for sql in (
                (
                    "INSERT INTO users (id, email) VALUES "
                    "(gen_random_uuid(), 'ro@example.com')"
                ),
                "UPDATE users SET display_name = 'x'",
                "DELETE FROM users",
                "CREATE TABLE ro_probe (id int)",
                "DROP TABLE users",
            ):
                with pytest.raises(DBAPIError) as err:
                    await conn.execute(text(sql))
                assert _sqlstate(err.value) == "25006", (
                    f"expected read_only_sql_transaction for {sql!r}, "
                    f"got {_sqlstate(err.value)}"
                )
                # Postgres aborts the transaction on error; restart it so the
                # next statement is tested under the same read-only guarantee.
                await conn.execute(text("ROLLBACK"))
                await conn.execute(text("BEGIN TRANSACTION READ ONLY"))
        finally:
            await conn.execute(text("ROLLBACK"))


# ──────────────────────────────────────────────────────────────────────
# Security: the schema holds no code
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_schema_contains_no_user_defined_triggers(pg_engine):
    """No business logic hides in the database, and this is the tripwire.

    Every rule in this product lives in Python where it is tested,
    reviewed and greppable. A trigger is code that runs on write with no
    call site — it would silently change what every one of the 2,000
    other tests means, and none of them run on postgres so none of them
    would notice. If this test fails, someone added one: it now needs
    tests of its own, and the failure is the reminder to write them.
    """
    async with pg_engine.connect() as conn:
        triggers = (
            (
                await conn.execute(
                    text(
                        "SELECT c.relname || '.' || t.tgname "
                        "FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        # tgisinternal excludes the triggers postgres creates
                        # itself to enforce foreign keys.
                        "WHERE NOT t.tgisinternal AND n.nspname = 'public'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert triggers == [], f"user-defined trigger(s) appeared: {triggers}"


@pytest.mark.asyncio
async def test_the_schema_contains_no_stored_procedures_of_our_own(pg_engine):
    """Same tripwire for functions and procedures.

    pgvector installs a few dozen functions of its own; those are
    excluded by their extension dependency (pg_depend deptype 'e'), so
    what is left is only what we wrote. Today that is nothing.
    """
    async with pg_engine.connect() as conn:
        routines = (
            (
                await conn.execute(
                    text(
                        "SELECT p.prokind::text || ':' || p.proname FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'public' AND NOT EXISTS ("
                        "  SELECT 1 FROM pg_depend d "
                        "  WHERE d.objid = p.oid AND d.deptype = 'e')"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert routines == [], f"user-defined routine(s) appeared: {routines}"


def test_no_migration_or_model_smuggles_in_a_trigger_or_function():
    """The catalog check above can only see what ``create_all`` built.

    A trigger created by a migration, or by a DDL event listener, would
    exist in production and never appear in the scratch database these
    tests build from the models. So this reads the source too. It needs
    no database, which is why it is a plain ``def``: it must keep working
    when postgres is not running.
    """
    pattern = re.compile(
        r"create\s+(or\s+replace\s+)?(trigger|function|procedure)\b", re.IGNORECASE
    )
    roots = [REPO_ROOT / "app", REPO_ROOT / "db"]
    offenders = []
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".sql"} or not path.is_file():
                continue
            text_ = path.read_text(encoding="utf-8", errors="replace")
            if pattern.search(text_):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        "SQL-level trigger/function DDL appeared in "
        f"{offenders} — it needs its own postgres tests"
    )


# ──────────────────────────────────────────────────────────────────────
# Backup and recovery
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _PgTools:
    """How to invoke pg_dump/pg_restore, and where they were found."""

    argv_prefix: list[str]
    conn_args: list[str]
    where: str
    env: dict[str, str] = field(default_factory=dict)

    def run(
        self, argv: list[str], stdin: bytes | None = None
    ) -> subprocess.CompletedProcess:
        """Run one pg_* tool, splicing the connection flags in after argv[0]."""
        return subprocess.run(
            [*self.argv_prefix, argv[0], *self.conn_args, *argv[1:]],
            input=stdin,
            capture_output=True,
            timeout=180,
            env={**os.environ, **self.env},
        )


def _container_with_pg_tools(port: int) -> str | None:
    """A running docker container that publishes `port` and has pg_dump."""
    if not shutil.which("docker"):
        return None
    try:
        listed = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    for line in listed.stdout.splitlines():
        name, _, ports = line.partition("\t")
        if f":{port}->" not in ports:
            continue
        try:
            probe = subprocess.run(
                ["docker", "exec", name, "pg_dump", "--version"],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return name
    return None


def _resolve_pg_tools(url: URL) -> _PgTools | None:
    """Host binaries if present, else the postgres container, else None."""
    if shutil.which("pg_dump") and shutil.which("pg_restore"):
        return _PgTools(
            argv_prefix=[],
            conn_args=[
                "-h",
                url.host or "localhost",
                "-p",
                str(url.port or 5432),
                "-U",
                url.username or "",
            ],
            env={"PGPASSWORD": url.password or ""},
            where="pg_dump/pg_restore on the host PATH",
        )
    container = _container_with_pg_tools(url.port or 5432)
    if container is not None:
        return _PgTools(
            # -i so pg_restore can read the dump from our stdin. Inside the
            # container the server is on the local socket, so no -h/-p.
            argv_prefix=["docker", "exec", "-i", container],
            conn_args=["-U", url.username or ""],
            where=f"pg_dump/pg_restore inside the {container!r} container",
        )
    return None


async def _shape_of(engine: AsyncEngine) -> dict[str, object]:
    """Tables, per-table row counts and constraints — what a restore must reproduce."""
    async with engine.connect() as conn:
        tables = sorted(
            (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                    )
                )
            )
            .scalars()
            .all()
        )
        counts = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            counts[table] = (
                await conn.execute(text(f"SELECT count(*) FROM {quoted}"))
            ).scalar_one()
        constraints = sorted(
            (
                await conn.execute(
                    text(
                        "SELECT c.conrelid::regclass::text || ':' || c.conname "
                        "|| ':' || c.contype::text FROM pg_constraint c "
                        "JOIN pg_namespace n ON n.oid = c.connamespace "
                        "WHERE n.nspname = 'public'"
                    )
                )
            )
            .scalars()
            .all()
        )
    return {"tables": tables, "counts": counts, "constraints": constraints}


@pytest.mark.asyncio
async def test_a_logical_dump_restores_into_a_second_database_intact(pg_engine, pg_url):
    """A backup nobody has restored is not a backup — restore one.

    THIS IS THE pg_dump PATH. It runs pg_dump -Fc against the scratch
    database, pipes the archive into pg_restore against a second, empty
    database, and then compares the two: same tables, same row counts
    per table, same set of constraints. That last one is the part a
    "the file is 400MB, looks fine" check misses — a restore that
    silently drops foreign keys leaves a database that accepts garbage.

    The binaries are looked for on PATH first and then inside the
    postgres container (the compose setup puts them there and not on the
    host). With neither available this SKIPS, and
    ``test_a_snapshot_schema_restores_rows_a_bad_delete_took_out`` still
    proves the in-database recovery path.
    """
    url = make_url(pg_url)
    tools = _resolve_pg_tools(url)
    if tools is None:
        pytest.skip(
            "no pg_dump/pg_restore on PATH and no running container that "
            "publishes the postgres port has them — the compose image "
            "carries the binaries, so start it or install postgresql-client"
        )

    source_db = url.database
    restore_db = f"{source_db}_restored"[:63]
    marker_ids: list[uuid.UUID] = []
    restored_engine: AsyncEngine | None = None

    try:
        # Rows have to be COMMITTED to appear in a dump, so this test writes
        # outside the rolled-back session fixture and cleans up after itself.
        maker = async_sessionmaker(bind=pg_engine, expire_on_commit=False)
        async with maker() as session:
            for _ in range(3):
                marker_ids.append((await make_user(session)).id)

        async with pg_engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{restore_db}"'))
            await conn.execute(text(f'CREATE DATABASE "{restore_db}"'))

        dumped = tools.run(["pg_dump", "-Fc", "-d", source_db])
        assert dumped.returncode == 0, (
            f"pg_dump failed ({tools.where}): {dumped.stderr.decode(errors='replace')}"
        )
        assert dumped.stdout, "pg_dump produced an empty archive"

        restored = tools.run(
            ["pg_restore", "--no-owner", "--no-privileges", "-d", restore_db],
            stdin=dumped.stdout,
        )
        assert restored.returncode == 0, (
            f"pg_restore failed: {restored.stderr.decode(errors='replace')}"
        )

        restored_engine = create_async_engine(url.set(database=restore_db))
        before = await _shape_of(pg_engine)
        after = await _shape_of(restored_engine)

        assert before["tables"], "nothing was dumped — the comparison would be vacuous"
        assert before["counts"]["users"] >= len(marker_ids)
        assert after["tables"] == before["tables"]
        assert after["counts"] == before["counts"]
        assert after["constraints"] == before["constraints"]
        # And the rows really came across, not just the counts.
        async with restored_engine.connect() as conn:
            survivors = (
                (await conn.execute(select(User.id).where(User.id.in_(marker_ids))))
                .scalars()
                .all()
            )
        assert sorted(survivors) == sorted(marker_ids)
    finally:
        if restored_engine is not None:
            await restored_engine.dispose()
        async with pg_engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": restore_db},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{restore_db}"'))
        if marker_ids:
            # Leave the shared scratch database exactly as it was found —
            # sibling test files run against it after this one.
            async with pg_engine.begin() as conn:
                await conn.execute(delete(User).where(User.id.in_(marker_ids)))


@pytest.mark.asyncio
async def test_a_snapshot_schema_restores_rows_a_bad_delete_took_out(pg_session):
    """THIS IS THE IN-DATABASE PATH, and it always runs.

    Before a risky backfill or a manual DELETE, the cheap insurance is
    ``CREATE TABLE snapshot.x AS SELECT * FROM x`` — one statement, no
    binaries, no filesystem. This test proves the round trip actually
    works on our schema: snapshot two tables, delete a user (whose
    settings row goes with it via ON DELETE CASCADE), restore both from
    the snapshot, and confirm the rows are back AND the foreign key
    between them is still enforced afterwards.

    That last check is the point. Restoring rows in the wrong order, or
    with constraints disabled, gives you a database that has the data
    and has lost the guarantee.
    """
    user = await make_user(pg_session)
    other = await make_user(pg_session)

    await pg_session.execute(text("CREATE SCHEMA recovery_snapshot"))
    await pg_session.execute(
        text("CREATE TABLE recovery_snapshot.users AS SELECT * FROM users")
    )
    await pg_session.execute(
        text(
            "CREATE TABLE recovery_snapshot.user_settings AS "
            "SELECT * FROM user_settings"
        )
    )

    # The accident: a DELETE that takes the settings row with it.
    await pg_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    remaining = (
        await pg_session.execute(
            text("SELECT count(*) FROM user_settings WHERE user_id = :id"),
            {"id": user.id},
        )
    ).scalar_one()
    assert remaining == 0, "expected ON DELETE CASCADE to remove the settings row"

    # The recovery: parent first, then child, so the FK is satisfiable at
    # every point — no constraint has to be dropped or deferred.
    await pg_session.execute(
        text("INSERT INTO users SELECT * FROM recovery_snapshot.users WHERE id = :id"),
        {"id": user.id},
    )
    await pg_session.execute(
        text(
            "INSERT INTO user_settings SELECT * FROM recovery_snapshot.user_settings "
            "WHERE user_id = :id"
        ),
        {"id": user.id},
    )

    restored = (
        await pg_session.execute(
            text(
                "SELECT u.email, s.currency FROM users u "
                "JOIN user_settings s ON s.user_id = u.id WHERE u.id = :id"
            ),
            {"id": user.id},
        )
    ).one()
    assert restored.email == user.email
    assert restored.currency == "USD"

    # The other user was never touched, and the restore did not duplicate it.
    total = (
        await pg_session.execute(
            text("SELECT count(*) FROM users WHERE id IN (:a, :b)"),
            {"a": user.id, "b": other.id},
        )
    ).scalar_one()
    assert total == 2

    # And the foreign key still bites after the restore: pointing the
    # restored settings row at a user that does not exist is refused with
    # 23503 (foreign_key_violation).
    savepoint = await pg_session.begin_nested()
    with pytest.raises(DBAPIError) as err:
        await pg_session.execute(
            text("UPDATE user_settings SET user_id = :ghost WHERE user_id = :id"),
            {"ghost": uuid.uuid4(), "id": user.id},
        )
    assert _sqlstate(err.value) == "23503"
    await savepoint.rollback()

    # Sanity: the ORM classes above are the ones these raw statements hit.
    assert User.__tablename__ == "users"
    assert UserSettings.__tablename__ == "user_settings"
