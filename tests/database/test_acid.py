"""ACID, asserted against the engine we actually deploy.

Every other test in this repo runs on SQLite, where "transaction" means one
writer at a time and "isolation level" means almost nothing. None of the
claims below can be made there: SQLite cannot show you a lost update
between two concurrent backends, cannot abort a transaction with 25P02, and
has no SSI to raise 40001. So these are the first tests in the suite that
say anything true about how our data behaves under concurrency.

The interesting half is ISOLATION, and specifically the LOST UPDATE — two
handlers reading a counter, both adding one, and the database ending up one
short. Postgres is not misbehaving when that happens; READ COMMITTED (the
server default here, asserted below) permits it. It is the application that
loses the write, by deciding the new value before it holds the row. That is
not a hypothetical here: it is what ``authenticate_with_password`` used to do
to the brute-force lockout counter, which is why the counter these tests move
is that exact column rather than something invented. It gets four tests: one
reproducing the loss with hand-written SQL, one for each of the two ways out,
and one driving the real sign-in path concurrently to prove the way out we
took is actually the one in production.

MECHANICS. Real concurrency needs real connections, so nothing here uses
the rolled-back ``pg_session``. Each test gets ``acid_engine``, a NullPool
engine on the scratch database with server-side timeouts set on every
connection. Owning the engine buys two things the shared ``pg_engine``
cannot: every ``connect()`` is a genuinely new backend rather than a
recycled one that might still hold the previous test's locks, and no
statement can wait forever. Everything these tests COMMIT hangs off one
throwaway user that ``acid_user`` deletes afterwards
(``collections.user_id`` is ON DELETE CASCADE, so the binder rows go
with it).

NO HANGS. A concurrency test that gets its locking wrong does not fail, it
stops — and a suite that hangs is a suite people kill. Two guards: every
wait is inside ``asyncio.wait_for``, and every connection is opened with
server-side ``statement_timeout``/``lock_timeout``, so the worst case is a
fast, loud failure.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.auth.passwords import hash_password
from app.config import get_settings
from app.services.auth import user_service

# How long a blocked statement is given to prove it is really blocked. Short:
# it is spent on every blocking test, and "still waiting after this" is the
# assertion, so it only has to outlast the round trip it competes with.
BLOCKED_FOR = 0.75

# How long that same statement gets to finish once the blocker commits. Long:
# nothing should ever need it, and if something does, the test fails instead
# of hanging the run.
RELEASED_WITHIN = 5.0

# Server-side backstop in case a test's own waiting is wrong. Comfortably
# longer than RELEASED_WITHIN so it never pre-empts a legitimate wait, and far
# shorter than a developer's patience.
_GUARD = {"statement_timeout": "10000", "lock_timeout": "10000"}


# ── helpers ──────────────────────────────────────────────────────────────


def _sqlstate(exc: BaseException) -> str | None:
    """The five-character SQLSTATE postgres actually returned.

    Asserting on the code rather than the message is deliberate: the message
    is prose that changes between releases, the SQLSTATE is the contract, and
    it is what application code has to branch on to retry a serialization
    failure rather than surface a 500.
    """
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


async def _insert_collection(
    conn: AsyncConnection, user_id: uuid.UUID, name: str
) -> None:
    await conn.execute(
        text(
            "INSERT INTO collections (id, user_id, name, is_public) "
            "VALUES (:id, :user_id, :name, false)"
        ),
        {"id": uuid.uuid4(), "user_id": user_id, "name": name},
    )


async def _count_collections(conn: AsyncConnection, user_id: uuid.UUID) -> int:
    result = await conn.execute(
        text("SELECT count(*) FROM collections WHERE user_id = :u"), {"u": user_id}
    )
    return int(result.scalar_one())


async def _read_counter(
    conn: AsyncConnection, user_id: uuid.UUID, *, for_update: bool = False
) -> int:
    """Read the brute-force lockout counter, optionally locking the row."""
    sql = "SELECT failed_login_count FROM users WHERE id = :u"
    if for_update:
        sql += " FOR UPDATE"
    result = await conn.execute(text(sql), {"u": user_id})
    return int(result.scalar_one())


async def _write_counter(conn: AsyncConnection, user_id: uuid.UUID, value: int) -> None:
    await conn.execute(
        text("UPDATE users SET failed_login_count = :v WHERE id = :u"),
        {"v": value, "u": user_id},
    )


async def _write_counter_in_own_transaction(
    conn: AsyncConnection, user_id: uuid.UUID, value: int
) -> None:
    await conn.begin()
    await _write_counter(conn, user_id, value)
    await conn.commit()


async def _final_counter(engine: AsyncEngine, user_id: uuid.UUID) -> int:
    """What the counter reads once every writer has gone home."""
    async with engine.connect() as conn:
        return await _read_counter(conn, user_id)


def _own_engine(url: URL | str) -> AsyncEngine:
    """A NullPool engine on the scratch database with the timeouts applied."""
    return create_async_engine(
        url, poolclass=NullPool, connect_args={"server_settings": dict(_GUARD)}
    )


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def acid_engine(pg_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """An engine on the scratch database that this test alone owns.

    Two reasons not to just use ``pg_engine``. NullPool: these tests reason
    about separate backends holding separate locks, and a pool that handed
    back a connection still sitting mid-transaction from the test before
    would turn a real failure into a baffling one — it also makes the
    durability test honest, since the "new connection" really is a new
    server process. And the ``_GUARD`` timeouts, which have to be set at
    connect time and which are what stop a mistake in one of the blocking
    tests from stalling the whole run.
    """
    engine = _own_engine(pg_engine.url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def acid_user(acid_engine: AsyncEngine) -> AsyncIterator[uuid.UUID]:
    """A committed user to hang committed test rows off, removed afterwards.

    These tests cannot use the rolled-back ``pg_session`` — the whole point
    is that a second connection sees (or does not see) the work — so they own
    their own cleanup. Deleting the user takes its collections with it.
    """
    user_id = uuid.uuid4()
    async with acid_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, is_admin, "
                "failed_login_count, token_version, mfa_enabled, plan, "
                "pro_trialing) VALUES (:id, :email, 'ACID probe', false, 0, 0, "
                "false, 'free', false)"
            ),
            {"id": user_id, "email": f"acid+{user_id.hex}@example.invalid"},
        )
    try:
        yield user_id
    finally:
        # lock_timeout is set on every connection from this engine, so a test
        # that failed while still holding the row makes teardown fail loudly
        # in seconds rather than wedging the run.
        async with acid_engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


# ── A: atomicity ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_transaction_that_raises_leaves_none_of_its_rows_behind(
    acid_engine: AsyncEngine, acid_user: uuid.UUID
):
    """All of the unit, or none of it — never the first two of three.

    This is the shape of every multi-row write we do: an import creates
    several binders and something after the second one throws. A user left
    owning half an import has to notice and clean it up by hand, which is a
    worse outcome than the import having plainly failed.

    The mid-transaction assertion matters as much as the final one: it proves
    the three rows genuinely existed, so the zero at the end is a rollback
    rather than three inserts that never ran.
    """
    names = [f"acid-atomic-{i}" for i in range(3)]

    with pytest.raises(RuntimeError, match="import blew up"):
        async with acid_engine.begin() as conn:
            for name in names:
                await _insert_collection(conn, acid_user, name)
            assert await _count_collections(conn, acid_user) == 3
            raise RuntimeError("import blew up after the third row")

    async with acid_engine.connect() as conn:
        assert await _count_collections(conn, acid_user) == 0


# ── C: consistency ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_constraint_violation_poisons_the_whole_transaction(
    acid_engine: AsyncEngine, acid_user: uuid.UUID
):
    """A rejected row does not just fail — it condemns everything with it.

    Two things are being pinned down here, and the second is the one that
    bites.

    First: the good insert that preceded the violation does not survive.
    Consistency is a property of the unit of work, not of each statement, so
    the database never lands in a state where half a rule holds.

    Second, and less obvious: postgres will not let the transaction limp on.
    Every further statement is refused with 25P02 until someone rolls back.
    Code that catches an IntegrityError and then keeps using the same session
    gets that error instead of the recovery it expected — which is why the
    only correct response to a constraint violation is to unwind.
    """
    async with acid_engine.connect() as conn:
        await conn.begin()
        await _insert_collection(conn, acid_user, "acid-consistency-good")
        assert await _count_collections(conn, acid_user) == 1

        # A collection belonging to a user that does not exist: the FK refuses.
        with pytest.raises(DBAPIError) as violation:
            await _insert_collection(conn, uuid.uuid4(), "acid-consistency-orphan")
        assert _sqlstate(violation.value) == "23503"  # foreign_key_violation

        # The transaction is now a brick wall, even for a trivial read.
        with pytest.raises(DBAPIError) as refused:
            await conn.exec_driver_sql("SELECT 1")
        assert _sqlstate(refused.value) == "25P02"  # in_failed_sql_transaction

        await conn.rollback()

        # Only now does it work again — and the good row went down with it.
        assert await _count_collections(conn, acid_user) == 0


# ── I: isolation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_scratch_database_isolates_at_read_committed(
    acid_engine: AsyncEngine,
):
    """Everything below is a claim about READ COMMITTED specifically.

    The lost update reproduced further down is *permitted* at this level and
    impossible at SERIALIZABLE, so if the server default ever changes, those
    tests are quietly asserting something else. Pin the premise.
    """
    async with acid_engine.connect() as conn:
        result = await conn.execute(text("SHOW default_transaction_isolation"))
        assert result.scalar_one() == "read committed"


@pytest.mark.asyncio
async def test_a_reader_sees_a_writers_row_only_once_the_writer_commits(
    acid_engine: AsyncEngine, acid_user: uuid.UUID
):
    """No dirty reads — and, at this level, no stale ones either.

    Half of this is the guarantee everyone expects: work in flight is
    private, so a request that is still deciding whether to fail cannot be
    observed by anybody else.

    The other half is the part that surprises people. The reader is inside a
    transaction of its own the whole time, and it still picks the new row up
    the moment the writer commits, because READ COMMITTED takes a fresh
    snapshot per STATEMENT, not per transaction. Any handler that reads a row
    twice and assumes the two answers agree is relying on a guarantee we do
    not have — that would be REPEATABLE READ.
    """
    async with acid_engine.connect() as writer, acid_engine.connect() as reader:
        await writer.begin()
        await reader.begin()

        await _insert_collection(writer, acid_user, "acid-uncommitted")

        assert await _count_collections(writer, acid_user) == 1, "writer sees its own"
        assert await _count_collections(reader, acid_user) == 0, "nobody else does"

        await writer.commit()

        # Same reader, same still-open transaction, different answer.
        assert await _count_collections(reader, acid_user) == 1
        await reader.rollback()


@pytest.mark.asyncio
async def test_a_second_update_of_the_same_row_waits_for_the_first_to_commit(
    acid_engine: AsyncEngine, acid_user: uuid.UUID
):
    """Row locks are held until COMMIT, not until the statement ends.

    This is the mechanism the next two tests are built on, so it is worth
    seeing on its own: an UPDATE takes an exclusive lock on the row and does
    not release it at the end of the statement, it releases it at the end of
    the TRANSACTION. A second writer is not rejected, it is parked.

    That is why one long transaction touching a hot row is a latency bug for
    everyone else touching it, and why the second writer's value is the one
    that survives: it is applied after the wait, not before it.
    """
    async with acid_engine.connect() as first, acid_engine.connect() as second:
        await first.begin()
        await _write_counter(first, acid_user, 1)  # takes the row lock

        blocked = asyncio.create_task(
            _write_counter_in_own_transaction(second, acid_user, 2)
        )
        # shield() so the timeout observes the wait without cancelling it — we
        # want to release the statement, not kill it mid-flight.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(blocked), timeout=BLOCKED_FOR)
        assert not blocked.done(), "the second UPDATE should still be waiting"

        await first.commit()  # release it

        await asyncio.wait_for(blocked, timeout=RELEASED_WITHIN)

    assert await _final_counter(acid_engine, acid_user) == 2


@pytest.mark.asyncio
async def test_two_unlocked_read_modify_writes_lose_an_increment(
    acid_engine: AsyncEngine, acid_user: uuid.UUID
):
    """The lost update: a counter incremented in Python cannot be trusted.

    THE RULE. Read a counter, decide its next value in application code, then
    write that value back, and the increment is only safe if nobody else
    touched the row in between — which at READ COMMITTED nothing guarantees.
    Any counter that matters has to be moved by the database.

    This is not a teaching example with invented columns. It is what
    ``authenticate_with_password`` used to do::

        user = await get_by_email(db, email)          # plain SELECT
        ...
        user.failed_login_count = (user.failed_login_count or 0) + 1
        await db.commit()                             # UPDATE ... SET n = 3

    — a read, a decision made in Python, and a write, with a gap in between.
    So: two sign-in attempts fail at the same moment. Each request reads
    ``failed_login_count`` as 0, each computes 0 + 1, each writes 1. Two
    failures happened; the counter says one. An attacker guessing down two
    connections at once therefore burned the lockout threshold at roughly
    half the rate it was configured for, which is the whole value of the
    control being quietly halved by a race.

    None of this is postgres misbehaving, which is why this test still asserts
    the loss: 1 is the correct answer for the SQL it writes, and the day it
    reads 2 the isolation level has changed underneath the whole file. At READ
    COMMITTED the second UPDATE waits for the first (see the test above) and
    then writes the value it had already decided on, exactly as the standard
    permits. The increment is lost in the gap, and it was lost by us.

    The fix is not a retry. It is either to hold the row across the gap
    (``SELECT ... FOR UPDATE``, the next test) or to never make the database
    hold a value you intend to overwrite (``SET n = n + 1``, the test after
    that). Sign-in took the second; what the service does now is asserted
    against the real function further down.
    """
    async with acid_engine.connect() as first, acid_engine.connect() as second:
        await first.begin()
        await second.begin()

        # Both read before either writes: the classic interleaving.
        first_read = await _read_counter(first, acid_user)
        second_read = await _read_counter(second, acid_user)
        assert (first_read, second_read) == (0, 0)

        await _write_counter(first, acid_user, first_read + 1)
        await first.commit()

        # Second already decided on 1, and writes it straight over the top.
        await asyncio.wait_for(
            _write_counter(second, acid_user, second_read + 1),
            timeout=RELEASED_WITHIN,
        )
        await second.commit()

    assert await _final_counter(acid_engine, acid_user) == 1, (
        "two increments, one survivor — if this ever reads 2 the isolation "
        "level changed and the FOR UPDATE test below is now proving nothing"
    )


@pytest.mark.asyncio
async def test_select_for_update_makes_those_same_two_writers_safe(
    acid_engine: AsyncEngine, acid_user: uuid.UUID
):
    """The same race, serialised — this time both increments survive.

    Identical to the test above in every respect except three words: the read
    is ``SELECT ... FOR UPDATE``. That moves the lock from after the decision
    to before it, so the second reader is parked until the first commits and
    then decides its increment from what the first actually wrote.

    The assertion in the middle is the one that explains why it works: the
    second connection's FOR UPDATE returns 1, not the 0 it would have read a
    moment earlier. At READ COMMITTED a locking read re-reads the row once it
    has the lock, so the second writer computes from fresh data.
    """
    async with acid_engine.connect() as first, acid_engine.connect() as second:
        await first.begin()
        await second.begin()

        first_read = await _read_counter(first, acid_user, for_update=True)
        assert first_read == 0

        # Second asks for the same lock and is put to sleep on the spot —
        # before it has decided anything, which is the whole trick.
        second_read_task = asyncio.create_task(
            _read_counter(second, acid_user, for_update=True)
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second_read_task), BLOCKED_FOR)
        assert not second_read_task.done()

        await _write_counter(first, acid_user, first_read + 1)
        await first.commit()

        second_read = await asyncio.wait_for(second_read_task, RELEASED_WITHIN)
        assert second_read == 1, "the lock was released onto the NEW row version"

        await _write_counter(second, acid_user, second_read + 1)
        await second.commit()

    assert await _final_counter(acid_engine, acid_user) == 2


@pytest.mark.asyncio
async def test_an_increment_done_in_one_statement_cannot_lose_a_write(
    acid_engine: AsyncEngine, acid_user: uuid.UUID
):
    """The other fix, and the cheaper one: never read the value at all.

    ``SET failed_login_count = failed_login_count + 1`` re-reads the row
    under the write lock it just took, so there is no window between the read
    and the write for a second writer to fit into. No explicit locking, no
    retry loop, no lost increment.

    It also rules out the dull explanation for the lost-update test — that
    one of its two UPDATEs simply never landed. Same two connections, same
    interleaving, same lock contention; only the arithmetic moved into the
    database, and now both increments survive.
    """
    bump = text(
        "UPDATE users SET failed_login_count = failed_login_count + 1 WHERE id = :u"
    )

    async with acid_engine.connect() as first, acid_engine.connect() as second:
        await first.begin()
        await second.begin()

        await first.execute(bump, {"u": acid_user})
        await first.commit()

        await asyncio.wait_for(
            second.execute(bump, {"u": acid_user}), timeout=RELEASED_WITHIN
        )
        await second.commit()

    assert await _final_counter(acid_engine, acid_user) == 2


@pytest.mark.asyncio
async def test_two_concurrent_failed_sign_ins_both_count_towards_the_lockout(
    acid_engine: AsyncEngine, acid_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
):
    """Two failed sign-ins landing together move the counter to 2, not 1.

    The three tests above reason about the hazard with hand-written SQL. This
    one drives the real ``authenticate_with_password`` down two connections at
    once, because that is the only version of the claim an attacker cares
    about: N parallel guesses must cost N counts, or the lockout threshold is
    reachable at a fraction of the rate it was configured for. It is the
    regression test that would have caught the original
    ``user.failed_login_count = (user.failed_login_count or 0) + 1``, which
    lands on 1 here.

    The barrier is what makes this deterministic rather than a coin flip. Both
    attempts have to have READ the user row before either WRITES — that is the
    interleaving the bug needs, and left to chance the argon2 verify (~100ms of
    blocking CPU) usually separates the two enough to hide it. Wrapping the
    real ``get_by_email`` is the smallest hook that pins it: everything after
    the lookup, including the increment under test, is untouched production
    code.

    Two failures is deliberately below ``login_max_attempts``, so this asserts
    the counting alone — no lock is stamped, no notification email is sent,
    and nothing here has to stub either.
    """
    settings = get_settings()
    assert settings.login_max_attempts > 2, "two failures must stay under the lock"

    # The probe user is created without a password; give it one so the real
    # sign-in path reaches the failure branch instead of the SSO-only branch.
    async with acid_engine.begin() as conn:
        email = (
            await conn.execute(
                text(
                    "UPDATE users SET password_hash = :h WHERE id = :u RETURNING email"
                ),
                {"h": hash_password("correct-horse-battery-staple"), "u": acid_user},
            )
        ).scalar_one()

    barrier = asyncio.Barrier(2)
    lookup = user_service.get_by_email

    async def read_then_wait(db: AsyncSession, addr: str):
        found = await lookup(db, addr)
        # wait_for keeps the no-hangs rule: a future change that stops calling
        # this once per attempt breaks the barrier loudly instead of parking
        # the suite forever.
        await asyncio.wait_for(barrier.wait(), timeout=RELEASED_WITHIN)
        return found

    monkeypatch.setattr(user_service, "get_by_email", read_then_wait)

    # Separate sessions on a NullPool engine, so each attempt really is its own
    # backend holding its own locks — the same shape as two web requests.
    maker = async_sessionmaker(bind=acid_engine, expire_on_commit=False)

    async def failed_attempt() -> None:
        async with maker() as db:
            result = await user_service.authenticate_with_password(
                db, email=email, password="definitely-not-the-password"
            )
            assert result is None, "the wrong password must not sign anyone in"

    await asyncio.wait_for(
        asyncio.gather(failed_attempt(), failed_attempt()),
        timeout=RELEASED_WITHIN * 4,  # two argon2 verifies plus a lock wait
    )

    assert await _final_counter(acid_engine, acid_user) == 2, (
        "an attempt was not counted — the increment is being computed in "
        "Python from a value the other attempt has already moved"
    )


@pytest.mark.asyncio
async def test_two_serializable_transactions_that_conflict_cannot_both_commit(
    acid_engine: AsyncEngine, acid_user: uuid.UUID
):
    """SERIALIZABLE does not block the race — it aborts the loser with 40001.

    Write skew, the hazard FOR UPDATE cannot help with because there is no
    row to lock: both transactions COUNT this user's binders to check a
    free-tier quota, both see room, both then INSERT. Neither touched the row
    the other wrote, so no row lock would ever have collided; under READ
    COMMITTED both commit and the quota is exceeded by one.

    Postgres's SSI notices that no serial order of the two produces this
    outcome and aborts the second committer with 40001. Two consequences
    worth writing down: SERIALIZABLE costs nothing until there is a genuine
    conflict (note that nothing blocks above), and it only protects anything
    if the caller RETRIES — a 40001 that escapes as a 500 has converted a
    solved problem back into a failed request.
    """
    first = await acid_engine.connect()
    second = await acid_engine.connect()
    try:
        # The isolation level has to be set before the transaction opens.
        first = await first.execution_options(isolation_level="SERIALIZABLE")
        second = await second.execution_options(isolation_level="SERIALIZABLE")
        await first.begin()
        await second.begin()

        # Both read the same set — this read is what SSI tracks.
        assert await _count_collections(first, acid_user) == 0
        assert await _count_collections(second, acid_user) == 0

        await _insert_collection(first, acid_user, "acid-serial-first")
        await _insert_collection(second, acid_user, "acid-serial-second")

        await first.commit()  # the winner, unaffected

        with pytest.raises(DBAPIError) as conflict:
            await second.commit()
        assert _sqlstate(conflict.value) == "40001"  # serialization_failure
    finally:
        await first.close()
        await second.close()

    # The loser left nothing behind: only the winner's binder exists.
    async with acid_engine.connect() as conn:
        assert await _count_collections(conn, acid_user) == 1


# ── D: durability ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_committed_row_outlives_the_connection_that_wrote_it(
    acid_engine: AsyncEngine, acid_user: uuid.UUID
):
    """Durability, scoped to what an in-process test can honestly claim.

    This does not test fsync. Nothing running on the same machine as the
    server can pull its power cord, and a test that pretended otherwise
    would be worse than no test at all.

    What it does test is the half applications get wrong: that COMMIT hands
    the row to the DATABASE, not to the session. The writing engine is
    disposed — every socket it held closed, its backend gone — and a brand
    new engine, connecting fresh, still sees the row. No part of that commit
    was living in the client.
    """
    name = f"acid-durable-{uuid.uuid4().hex[:8]}"

    writer = _own_engine(acid_engine.url)
    async with writer.begin() as conn:
        await _insert_collection(conn, acid_user, name)
    await writer.dispose()  # the connection that wrote it no longer exists

    reader = _own_engine(acid_engine.url)
    try:
        async with reader.connect() as conn:
            result = await conn.execute(
                text("SELECT name FROM collections WHERE user_id = :u"),
                {"u": acid_user},
            )
            assert [row[0] for row in result] == [name]
    finally:
        await reader.dispose()
