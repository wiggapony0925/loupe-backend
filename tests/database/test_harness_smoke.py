"""The harness itself.

Everything else in this directory depends on these three fixtures, so they
get their own tests: a broken fixture otherwise shows up as six unrelated
files failing for six unrelated-looking reasons.

The isolation guarantee is the one worth pinning. These tests run against a
scratch database created for the session, and a bug that let them run
against the developer's `loupe` database instead would be silent — the
tests would still pass, while writing to and locking the database someone
was working in. So it is asserted rather than assumed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from app.db import Base


@pytest.mark.asyncio
async def test_the_suite_runs_against_a_scratch_database_not_the_real_one(pg_engine):
    """The whole isolation promise, in one assertion."""
    async with pg_engine.connect() as conn:
        name = (await conn.execute(text("SELECT current_database()"))).scalar()
    assert name is not None
    assert name.startswith("loupe_dbtest_"), (
        f"database tests are pointed at {name!r} — they must only ever run "
        "against the scratch database created by the pg_url fixture"
    )


@pytest.mark.asyncio
async def test_the_scratch_database_has_the_whole_model_schema(pg_engine):
    """create_all ran, so a missing table later means a model changed —
    not that the fixture forgot to build anything."""
    async with pg_engine.connect() as conn:
        found = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    expected = set(Base.metadata.tables)
    assert expected - found == set(), (
        f"missing from the scratch DB: {sorted(expected - found)}"
    )


@pytest.mark.asyncio
async def test_pgvector_is_available(pg_engine):
    """The catalog embeddings column needs the extension; without it
    create_all would have failed, but the failure would read as a type
    error rather than a missing extension."""
    async with pg_engine.connect() as conn:
        ext = (
            await conn.execute(
                text("SELECT extname FROM pg_extension WHERE extname='vector'")
            )
        ).scalar()
    assert ext == "vector"


@pytest.mark.asyncio
async def test_a_sessions_writes_are_rolled_back_between_tests(pg_session):
    """Writes the next test must not see. This one makes the row..."""
    from tests.factories import make_user

    user = await make_user(pg_session)
    await pg_session.flush()
    assert user.id is not None
    # Stashed on the class so the sibling test below can look for it without
    # depending on test ordering beyond "same module, declared after".
    TestState.leaked_id = str(user.id)


class TestState:
    leaked_id: str | None = None


@pytest.mark.asyncio
async def test_the_previous_tests_row_is_gone(pg_session):
    """...and this one proves it never landed. If this fails, every test in
    the directory is sharing state and their results are meaningless."""
    if TestState.leaked_id is None:
        pytest.skip("ordering-dependent; only meaningful after the writer test")
    row = (
        await pg_session.execute(
            text("SELECT count(*) FROM users WHERE id = :i"),
            {"i": TestState.leaked_id},
        )
    ).scalar()
    assert row == 0, (
        "a previous test's row survived — the rollback fixture is not isolating"
    )
