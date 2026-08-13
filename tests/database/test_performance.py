"""What the postgres query planner actually does with the schema we ship.

WHY THIS FILE EXISTS. Every other test in the repo runs on SQLite, which
has no query planner worth the name and never sees an index. So no test
has ever noticed that a query the app runs on every feed load falls back
to reading a whole table — and none ever will while the assertions are
about rows returned rather than how they were found.

WHY THERE ARE NO TIMINGS HERE (with one deliberate exception). An
assertion on milliseconds is a coin flip on a laptop with a browser open,
and it is meaningless on a scratch database with a few thousand rows: a
seq scan over 5,000 rows is fast, and it is still the bug. So these tests
read the PLAN — ``EXPLAIN (FORMAT JSON)`` — and assert on its SHAPE. A
plan is a fact about the schema, identical on this machine and in
production; a duration is a fact about this machine.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.social.models import SocialPost
from tests.factories import make_user

# ---------------------------------------------------------------------------
# Deriving index coverage from the catalog
# ---------------------------------------------------------------------------

# Every foreign key in the schema, with the FIRST column of the constraint
# and what a delete of the parent row does. ``k.ord = 1`` keeps us to the
# leading column: for a composite FK that is the only one an index has to
# lead with for the constraint check to be able to use it.
_FOREIGN_KEYS = text("""
    SELECT con.conrelid::regclass::text AS tbl,
           att.attname                  AS col,
           con.confdeltype::text        AS on_delete
    FROM pg_constraint con
    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
      ON k.ord = 1
    JOIN pg_attribute att
      ON att.attrelid = con.conrelid AND att.attnum = k.attnum
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid = rel.relnamespace
    WHERE con.contype = 'f' AND ns.nspname = 'public'
""")

# Every (table, column) that some index LEADS with. Leading is the whole
# point: an index on (user_id, captured_at) can answer "where user_id = ?"
# and an index on (captured_at, user_id) cannot.
#
#   * ``indisvalid`` — a failed CREATE INDEX CONCURRENTLY leaves an index
#     the planner refuses to use. It must not count as coverage.
#   * ``indpred IS NULL`` — a partial index only covers the rows matching
#     its predicate, so it cannot be relied on for an arbitrary lookup or
#     for a referential-integrity check.
#   * btree/hash — the access methods that answer equality. A gin or
#     ivfflat index (we have pgvector installed) leading with a column
#     does nothing for a foreign-key check.
_INDEX_LEADING_COLUMNS = text("""
    SELECT idx.indrelid::regclass::text AS tbl,
           att.attname                  AS col
    FROM pg_index idx
    JOIN pg_class ic ON ic.oid = idx.indexrelid
    JOIN pg_am am ON am.oid = ic.relam
    JOIN pg_class rel ON rel.oid = idx.indrelid
    JOIN pg_namespace ns ON ns.oid = rel.relnamespace
    JOIN pg_attribute att
      ON att.attrelid = idx.indrelid AND att.attnum = idx.indkey[0]
    WHERE ns.nspname = 'public'
      AND idx.indisvalid
      AND idx.indpred IS NULL
      AND am.amname IN ('btree', 'hash')
""")

_ON_DELETE = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}

# THE ALLOWLIST, AND IT IS EMPTY. Postgres creates an index for a primary
# key and for a unique constraint, and NEVER for a foreign key — that is
# left to you. Every foreign key in this schema now has one; migration
# 0056 added the last seven.
#
# It stays here, empty, rather than being deleted along with them: the
# test below is a ratchet, and this is where a deliberate exception would
# go. If some future table genuinely does not want the index — a tiny
# lookup table, a write-heavy column whose parent is never deleted — put
# it here with the ON DELETE action and a reason, so the exception is a
# decision somebody made rather than an oversight nobody noticed.
UNINDEXED_FOREIGN_KEYS: dict[tuple[str, str], str] = {}


# ---------------------------------------------------------------------------
# EXPLAIN helpers
# ---------------------------------------------------------------------------


async def _plan(db: AsyncSession, sql: str, **params: Any) -> dict[str, Any]:
    """Return the root node of the plan postgres would use for `sql`.

    ``EXPLAIN`` and not ``EXPLAIN ANALYZE``: we want the plan, not a
    stopwatch, and nothing here should have side effects.
    """
    raw = (await db.execute(text(f"EXPLAIN (FORMAT JSON) {sql}"), params)).scalar()
    doc = json.loads(raw) if isinstance(raw, str) else raw
    return doc[0]["Plan"]


def _nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Every node in the plan tree, depth first."""
    found = [plan]
    for child in plan.get("Plans", []):
        found.extend(_nodes(child))
    return found


def _scans_of(plan: dict[str, Any], table: str) -> list[str]:
    """The node types that read `table`, e.g. ``['Bitmap Heap Scan']``."""
    return [n["Node Type"] for n in _nodes(plan) if n.get("Relation Name") == table]


def _indexes_used(plan: dict[str, Any]) -> set[str]:
    return {n["Index Name"] for n in _nodes(plan) if n.get("Index Name")}


def _shape(plan: dict[str, Any]) -> str:
    """A one-line rendering of the plan, for assertion messages.

    A failure here should tell you what postgres decided to do, not just
    that it wasn't what you hoped.
    """
    parts = []
    for node in _nodes(plan):
        label = node["Node Type"]
        if node.get("Relation Name"):
            label += f" on {node['Relation Name']}"
        if node.get("Index Name"):
            label += f" using {node['Index Name']}"
        parts.append(label)
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

# Enough rows that the planner has a real choice to make. This is the
# whole reason the number is not 5: on a tiny table postgres correctly
# reads the entire thing rather than bothering with an index, so a test
# that seeded a handful of rows and then asserted "Index Scan" would be
# asserting nothing at all — and a test asserting "Seq Scan" on a tiny
# table would pass whether or not the index existed. The rows are also
# spread thinly over many parents (10 posts per author) so that any one
# lookup is a small slice of the table, which is when an index wins.
SEED_POSTS = 5_000
SEED_PARENTS = 500


async def _seed_social_graph(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Fill users/cards/social_posts/social_post_comments and ANALYZE.

    Written as set-based INSERTs rather than through the factories on
    purpose: 500 factory users would be 500 round trips and 500 commits,
    and none of what this file asserts depends on how the rows were made.

    ANALYZE is the other half. Without statistics postgres plans against
    a hardcoded guess about table size, so the plans below would be
    testing the default rather than the schema. Both the inserts and the
    statistics live inside the fixture's transaction and are rolled back
    with everything else.

    Returns an (author_id, card_id) pair to look rows up by.
    """
    await db.execute(
        text("""
        INSERT INTO users (id, email, is_admin, failed_login_count,
                           token_version, mfa_enabled, plan, pro_trialing)
        SELECT gen_random_uuid(), 'perf-' || g || '@example.test',
               false, 0, 0, false, 'free', false
        FROM generate_series(1, :n) AS g
        """),
        {"n": SEED_PARENTS},
    )
    await db.execute(
        text("""
        INSERT INTO card_sets (id, tcg, name, code, created_at)
        VALUES (gen_random_uuid(), 'pokemon', 'Perf Set', 'PRF', now())
        """)
    )
    await db.execute(
        text("""
        INSERT INTO cards (id, set_id, tcg, name)
        SELECT gen_random_uuid(), (SELECT id FROM card_sets LIMIT 1),
               'pokemon', 'Perf Card ' || g
        FROM generate_series(1, :n) AS g
        """),
        {"n": SEED_PARENTS},
    )
    # Deal the posts round-robin across the authors and the cards.
    await db.execute(
        text("""
        WITH numbered_users AS (
            SELECT id, row_number() OVER (ORDER BY email) - 1 AS n FROM users
        ), numbered_cards AS (
            SELECT id, row_number() OVER (ORDER BY name) - 1 AS n FROM cards
        )
        INSERT INTO social_posts (id, author_id, card_id, body)
        SELECT gen_random_uuid(), u.id, c.id, 'perf post ' || g
        FROM generate_series(0, :n - 1) AS g
        JOIN numbered_users u ON u.n = mod(g, :p)
        JOIN numbered_cards c ON c.n = mod(g, :p)
        """),
        {"n": SEED_POSTS, "p": SEED_PARENTS},
    )
    await db.execute(
        text("""
        INSERT INTO social_post_comments (id, post_id, author_id, body)
        SELECT gen_random_uuid(), p.id, p.author_id, 'perf comment'
        FROM social_posts p, generate_series(1, 2) AS g
        """)
    )
    await db.execute(text("ANALYZE users, cards, social_posts, social_post_comments"))

    author_id = (
        await db.execute(text("SELECT author_id FROM social_posts LIMIT 1"))
    ).scalar_one()
    card_id = (
        await db.execute(text("SELECT card_id FROM social_posts LIMIT 1"))
    ).scalar_one()
    return author_id, card_id


# ---------------------------------------------------------------------------
# 1. Index coverage for foreign keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_foreign_key_has_an_index_leading_with_its_column(
    pg_session: AsyncSession,
) -> None:
    """Every foreign key column leads a btree. Postgres will not do this for you.

    It indexes primary keys and unique constraints and never foreign keys,
    and that asymmetry is the most common way a schema quietly acquires a
    full table scan. An unindexed FK column costs twice:

    * every lookup or join THROUGH it reads the whole child table, and
    * every DELETE or UPDATE of the PARENT row does too, because postgres
      has to check the referencing rows — even when nothing references it.

    Seven of them were unindexed until 0056, and two are worth naming
    because they are the shapes that recur.
    ``portfolio_snapshots.collection_id`` is ``ON DELETE CASCADE``, so
    deleting a single collection made postgres scan the entire snapshots
    table to find the rows to cascade into — and that table grows by up to
    ~48 rows per user per day, unbounded by the number of collections, so
    the cost of "delete a binder" grew with how long the whole product had
    been running. ``collection_items.graded_card_id`` is the same trap in
    a subtler dress: the column IS in the primary key, but it is the
    SECOND column of it, and an index on (collection_id, graded_card_id)
    cannot answer "which rows reference this graded card?" — so deleting a
    card from a vault scanned every collection_items row in the database.

    The ratchet still works in both directions. A new unindexed foreign
    key fails here before the scan ships; an index added for one of the
    allowlisted exceptions (there are none today) fails too, telling you
    to shorten the list.
    """
    fks = (await pg_session.execute(_FOREIGN_KEYS)).all()
    leading = {
        (row.tbl, row.col)
        for row in (await pg_session.execute(_INDEX_LEADING_COLUMNS)).all()
    }
    assert fks, "no foreign keys found — the schema did not get built"

    actual = {
        (row.tbl, row.col): _ON_DELETE[row.on_delete]
        for row in fks
        if (row.tbl, row.col) not in leading
    }

    now_indexed = sorted(set(UNINDEXED_FOREIGN_KEYS) - set(actual))
    assert not now_indexed, (
        "good news, this list shrank — these foreign keys now have an index "
        f"leading with them: {now_indexed}. Delete them from "
        "UNINDEXED_FOREIGN_KEYS so the ratchet keeps holding."
    )
    newly_unindexed = sorted(set(actual) - set(UNINDEXED_FOREIGN_KEYS))
    assert not newly_unindexed, (
        f"new unindexed foreign key(s): {newly_unindexed}. Every lookup "
        "through these columns, and every delete of the parent row, reads "
        "the whole child table. Add an index leading with the column, or "
        "add it to UNINDEXED_FOREIGN_KEYS with a reason."
    )
    assert actual == UNINDEXED_FOREIGN_KEYS, (
        "the ON DELETE action of an unindexed foreign key changed — which "
        "changes how much the missing index costs: "
        f"{sorted(set(actual.items()) ^ set(UNINDEXED_FOREIGN_KEYS.items()))}"
    )


# ---------------------------------------------------------------------------
# 2. Sanity: we can read a plan at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lookup_by_primary_key_is_an_index_scan(
    pg_session: AsyncSession,
) -> None:
    """The control for every other test in this file.

    If EXPLAIN parsing were broken, or the fixture handed us a database
    with no schema in it, the interesting assertions below would fail for
    reasons that have nothing to do with indexes. A primary key lookup is
    the one plan postgres will never get wrong, so if this passes, a
    failure elsewhere is a real finding.
    """
    user = await make_user(pg_session)

    plan = await _plan(pg_session, "SELECT * FROM users WHERE id = :uid", uid=user.id)

    assert "Seq Scan" not in _scans_of(plan, "users"), _shape(plan)
    assert "pk_users" in _indexes_used(plan), _shape(plan)


# ---------------------------------------------------------------------------
# 3. Both of one table's foreign keys, same query shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_post_lookup_seeks_by_author_and_by_card_rather_than_reading_the_feed(
    pg_session: AsyncSession,
) -> None:
    """Both of a post's parents can be looked up without reading the table.

    ``social_posts`` carries two foreign keys to two different parents, and
    until 0056 only one of them was usable: ``author_id`` led
    ``ix_social_posts_author_created`` while ``card_id`` led nothing, so
    "posts by this author" seeked straight to its rows and "posts
    showcasing this card" — the query behind a card page's community
    section — read every post in the table. This is the demonstration
    that the entry is gone from UNINDEXED_FOREIGN_KEYS for a reason: the
    two queries now plan the same way.

    Asserted through the plan and not a stopwatch, and with enough seeded
    rows that the planner has a real choice — on a table of five rows a
    sequential scan is the CORRECT plan and this test would prove nothing.
    """
    author_id, card_id = await _seed_social_graph(pg_session)

    by_author = await _plan(
        pg_session,
        "SELECT id FROM social_posts WHERE author_id = :aid",
        aid=author_id,
    )
    assert "Seq Scan" not in _scans_of(by_author, "social_posts"), _shape(by_author)
    assert "ix_social_posts_author_created" in _indexes_used(by_author), _shape(
        by_author
    )

    by_card = await _plan(
        pg_session,
        "SELECT id FROM social_posts WHERE card_id = :cid",
        cid=card_id,
    )
    assert "Seq Scan" not in _scans_of(by_card, "social_posts"), _shape(by_card)
    assert "ix_social_posts_card_id" in _indexes_used(by_card), _shape(by_card)


# ---------------------------------------------------------------------------
# 4. The hottest join in the product
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_profile_thread_join_never_scans_posts_or_comments(
    pg_session: AsyncSession,
) -> None:
    """users -> social_posts -> social_post_comments is the path that must not scan.

    This is the shape behind a profile and its threads, and it is the
    query that runs most often in the product. It is also the one where a
    missing index hurts most: both child tables grow forever, so a plan
    that reads them end to end gets slower every day the app is up, on the
    screen users open first.

    Both children have to be reached through an index — ``author_id`` via
    ``ix_social_posts_author_created``, ``post_id`` via
    ``ix_social_post_comments_post_created`` — for that to hold. The
    assertion is about the child tables specifically: postgres is free to
    scan ``users`` if it ever decides that is cheaper, and that would be
    a fine decision.
    """
    author_id, _ = await _seed_social_graph(pg_session)

    plan = await _plan(
        pg_session,
        """
        SELECT c.id, c.body, p.id
        FROM users u
        JOIN social_posts p ON p.author_id = u.id
        JOIN social_post_comments c ON c.post_id = p.id
        WHERE u.id = :uid
        ORDER BY c.created_at DESC
        LIMIT 50
        """,
        uid=author_id,
    )

    assert "Seq Scan" not in _scans_of(plan, "social_posts"), _shape(plan)
    assert "Seq Scan" not in _scans_of(plan, "social_post_comments"), _shape(plan)
    assert {
        "ix_social_posts_author_created",
        "ix_social_post_comments_post_created",
    } <= _indexes_used(plan), _shape(plan)


# ---------------------------------------------------------------------------
# 5. Load
# ---------------------------------------------------------------------------

# The ONE wall-clock assertion in this file, and it is deliberately
# useless as a benchmark. The ceiling is two orders of magnitude above
# what this takes on a developer laptop (~0.15s), so it cannot fail
# because someone was compiling in another window; it can only fail if
# writing rows has become pathological —
# a per-row round trip where there was a batch, an accidentally quadratic
# path, a trigger nobody meant to add. Do not tighten it into a
# performance target: a number that fails on a busy machine is a number
# people learn to rerun until it passes.
BULK_ROWS = 5_000
BULK_CEILING_SECONDS = 30.0


@pytest.mark.asyncio
async def test_writing_a_few_thousand_posts_in_one_transaction_stays_sane(
    pg_session: AsyncSession,
) -> None:
    """A smoke test for a pathological write regression, not a benchmark.

    Bulk writes are real here — catalog sync and the seeders push far more
    than this in a single transaction — and the failure mode worth
    catching is categorical, not marginal: something that turns one
    batched statement into five thousand round trips is 100x slower, not
    10% slower, and it will sail past any test that only checks the rows
    landed.
    """
    author = await make_user(pg_session)

    started = time.perf_counter()
    pg_session.add_all(
        [
            SocialPost(author_id=author.id, body=f"bulk post {n}")
            for n in range(BULK_ROWS)
        ]
    )
    await pg_session.flush()
    elapsed = time.perf_counter() - started

    written = (
        await pg_session.execute(
            text("SELECT count(*) FROM social_posts WHERE author_id = :aid"),
            {"aid": author.id},
        )
    ).scalar_one()
    assert written == BULK_ROWS
    assert elapsed < BULK_CEILING_SECONDS, (
        f"writing {BULK_ROWS} rows in one transaction took {elapsed:.1f}s, "
        f"over the {BULK_CEILING_SECONDS:.0f}s ceiling. This ceiling is very "
        "generous, so treat it as a real regression in the write path rather "
        "than a slow machine."
    )
