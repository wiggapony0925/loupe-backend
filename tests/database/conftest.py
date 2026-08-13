"""Postgres-backed database tests.

WHY THIS DIRECTORY EXISTS. Every other test in this repo runs against
SQLite in memory (see the root conftest). That is the right trade for
2,000 API tests — it is fast and hermetic — but it means nothing in the
suite has ever exercised the engine we actually deploy. SQLite does not
enforce the same constraints, does not have our types, has one writer and
no isolation levels to speak of, and never sees an index. CI even
provisions a postgres 16 service and then points DATABASE_URL at SQLite,
so the container starts and is never connected to.

So these tests do the opposite: they talk to a REAL postgres and assert
the things only postgres can answer — that the schema in the database
matches the models, that constraints actually refuse bad rows, that
cascades really cascade, that transactions isolate, and that the indexes
the query planner needs are there.

ISOLATION. The suite creates its own database (``loupe_dbtest_<pid>``) on
the same server, builds the schema into it, and drops it at the end. It
never reads, writes or locks anything in the developer's ``loupe``
database — a test suite that can corrupt the database you were working in
is one people stop running.

EVENT LOOPS, which is the subtle part. pytest-asyncio runs each test in a
fresh event loop, and an asyncpg connection belongs to the loop that
opened it. So the expensive one-time setup (create the database, build 57
tables) happens in a SYNCHRONOUS fixture that owns its own short-lived
loop via ``asyncio.run``, while the engine every test actually uses is
function-scoped and therefore always bound to the loop currently running.
A session-scoped async engine looks tidier and fails on the second test
with "attached to a different loop".

SKIPPING. With no postgres reachable, every test here skips with a reason
rather than failing. The rest of the suite is unaffected, and CI can turn
these on by pointing LOUPE_TEST_PG_URL at the service it already starts.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

# Importing the models is what REGISTERS them on Base.metadata. Without this
# the metadata is empty when create_all runs, the scratch database comes up
# with zero tables, and the failure lands later as "table does not exist" in
# whichever test happens to run first. Same reason alembic's 0001 does it.
from app import models  # noqa: F401
from app.db import Base
from app.social import models as social_models  # noqa: F401

# The server to create the scratch database ON. Defaults to the local docker
# compose postgres; CI overrides it to the service it already provisions.
ADMIN_URL = os.environ.get(
    "LOUPE_TEST_PG_URL",
    "postgresql+asyncpg://loupe:loupe@localhost:5433/loupe",
)

# Unique per process so parallel runs (and a developer running the suite while
# CI runs it too) cannot collide on the same scratch database.
TEST_DB = f"loupe_dbtest_{os.getpid()}_{uuid.uuid4().hex[:6]}"


def _with_database(url: str, name: str) -> str:
    head, _, _ = url.rpartition("/")
    return f"{head}/{name}"


def _redacted(url: str) -> str:
    """host:port/db — never the password, even in a skip message."""
    return url.rsplit("@", 1)[-1]


async def _create_scratch() -> None:
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    finally:
        await admin.dispose()

    engine = create_async_engine(_with_database(ADMIN_URL, TEST_DB))
    try:
        async with engine.begin() as conn:
            # The catalog embedding column is a pgvector type; without the
            # extension create_all fails as an unknown-type error that reads
            # like a model bug rather than a missing extension.
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _drop_scratch() -> None:
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            # Terminate stragglers first: one open connection makes DROP fail
            # and leaves scratch databases piling up on the dev machine.
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": TEST_DB},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
    finally:
        await admin.dispose()


async def _probe() -> str | None:
    """None when postgres answers; otherwise why it did not."""
    engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """Create the scratch database once, yield its URL, drop it at the end.

    Deliberately a SYNC fixture: ``asyncio.run`` gives the setup its own
    loop which is closed before any test starts, so nothing long-lived is
    bound to a loop that will not exist by the time tests run.
    """
    reason = asyncio.run(_probe())
    if reason is not None:
        pytest.skip(f"no postgres at {_redacted(ADMIN_URL)} — {reason}")

    asyncio.run(_create_scratch())
    try:
        yield _with_database(ADMIN_URL, TEST_DB)
    finally:
        asyncio.run(_drop_scratch())


@pytest_asyncio.fixture
async def pg_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    """An engine on the scratch database, bound to THIS test's loop.

    Function-scoped on purpose — see the note on event loops above. The
    schema is already built by ``pg_url``, so this only pays connection
    setup, not 57 CREATE TABLEs.
    """
    engine = create_async_engine(pg_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def pg_session(pg_engine: AsyncEngine) -> AsyncIterator:
    """A session whose work is always rolled back.

    Every test runs inside a transaction that is discarded, so tests cannot
    see each other's rows and the scratch schema stays pristine without a
    truncate between tests. Tests that need to observe a COMMIT (durability,
    isolation) take ``pg_engine`` and open their own connections instead.
    """
    async with pg_engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        session = maker()
        try:
            yield session
        finally:
            await session.close()
            if trans.is_active:
                await trans.rollback()
