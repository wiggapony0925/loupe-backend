"""Drop five duplicated unique constraints and one index that cannot be used.

FIVE DUPLICATE PAIRS. Five tables carry the same column indexed twice, and
both copies are UNIQUE, so postgres has been maintaining two identical btrees
on every insert and update:

    blog_posts.slug            ix_blog_posts_slug            + uq_blog_posts_slug
    catalog_image_hashes.
        upstream_id            ix_..._upstream_id            + uq_..._upstream_id
    feature_flags.key          ix_feature_flags_key          + uq_feature_flags_key
    job_postings.slug          ix_job_postings_slug          + uq_job_postings_slug
    waitlist_entries.email     ix_waitlist_entries_email     + uq_waitlist_entries_email

WHY THE ``uq_`` SIDE IS THE ONE THAT GOES, which is the part worth reading
before approving this. The instinct is to keep the constraint and drop the
"plain" index. That is backwards here, for two independent reasons.

First, the models never declared the constraint. ``mapped_column(unique=True,
index=True)`` in SQLAlchemy emits a single UNIQUE INDEX named ``ix_*`` — not a
UniqueConstraint — so ``Base.metadata`` for all five tables lists ``ix_*`` and
no ``uq_*`` at all. The ``uq_*`` constraints came from migrations that spelled
``sa.UniqueConstraint(...)`` by hand (0012 for blog_posts, 0014 for
feature_flags, and the same pattern after). Production therefore carries five
objects the code does not know about, while a database built by ``create_all``
— every local and CI database, see 0001 — has only the ``ix_*`` side. Dropping
``uq_*`` closes that drift. Dropping ``ix_*`` would widen it, and the
tests/database suite exists precisely to catch this class of divergence.

Second, ``ix_*`` is the side actually carrying the traffic. Over 88 days of
production statistics ``ix_catalog_image_hashes_upstream_id`` served 415,460
scans against 146,925 for its twin — the planner splits between two
indistinguishable indexes, so both look "used" and neither is load-bearing on
its own. ``ix_feature_flags_key`` shows 1,046 scans to its twin's zero.

The uniqueness guarantee is unchanged: a UNIQUE INDEX enforces exactly what a
UNIQUE CONSTRAINT enforces. The only thing lost is the ability for a foreign
key to name the constraint as its target, and no foreign key does — checked
against pg_constraint, `conindid` matches none of the five.

THE PHASH INDEX — 16 MB THAT CANNOT ANSWER ITS OWN QUESTION.
``ix_catalog_image_hashes_phash`` is a plain btree on a 64-character hex
string, 16 MB, and has been scanned zero times in 88 days. The model comment
justified it as "the first 4 hex are indexed for a cheap prefix pre-filter
should the in-memory cache ever be bypassed". That is not what a default btree
does on this database. The cluster collation is en_US.UTF8, so a prefix
predicate does not become an index range scan without ``varchar_pattern_ops``,
and the planner confirms it:

    EXPLAIN SELECT id FROM catalog_image_hashes WHERE phash LIKE 'abcd%'
    ->  Seq Scan on catalog_image_hashes  (cost=0.00..8031.94 rows=13)

So the fallback the comment describes would sequentially scan whether or not
this index exists. What the index CAN serve is equality on the full 64-char
hash — a lookup perceptual matching never performs, because the entire point
of a perceptual hash is that near-matches differ in some bits. Matching runs
in memory over every hash (`catalog_hash_index.py`, 10-minute TTL cache) using
Hamming distance, which no btree can answer. The zero scan count is not an
under-used index; it is an index with no expressible caller.

If a prefix pre-filter is ever genuinely wanted, the index for it is
``(phash varchar_pattern_ops)`` and it should arrive with the query that uses
it. The model's ``index=True`` and its comment are corrected in the same
change as this revision.

COST. Nothing here rewrites a table. DROP CONSTRAINT and DROP INDEX are
catalog operations that take a brief ACCESS EXCLUSIVE lock and return in
milliseconds; the space (~10 MB from the duplicate pair, 16 MB from phash) is
returned when the index files are unlinked. This is safe to run online.
``CONCURRENTLY`` is deliberately not used — it cannot run inside a
transaction, and env.py wraps the upgrade in one.

Revision ID: 0058_drop_redundant_indexes
Revises: 0057_json_to_jsonb
"""

from __future__ import annotations

from alembic import op

revision = "0058_drop_redundant_indexes"
down_revision = "0057_json_to_jsonb"
branch_labels = None
depends_on = None


#: (constraint, table, column). The ``ix_*`` unique index on the same column
#: stays and keeps enforcing uniqueness.
REDUNDANT_UNIQUE_CONSTRAINTS: list[tuple[str, str, str]] = [
    ("uq_blog_posts_slug", "blog_posts", "slug"),
    ("uq_catalog_image_hashes_upstream_id", "catalog_image_hashes", "upstream_id"),
    ("uq_feature_flags_key", "feature_flags", "key"),
    ("uq_job_postings_slug", "job_postings", "slug"),
    ("uq_waitlist_entries_email", "waitlist_entries", "email"),
]

UNUSABLE_INDEX = ("ix_catalog_image_hashes_phash", "catalog_image_hashes", "phash")


def _is_postgres() -> bool:
    """DROP CONSTRAINT is postgres DDL; SQLite cannot do it without a rebuild.

    Same guard as 0056, and for the same reason: every deployment is postgres,
    and the only non-postgres caller is a migration replay running on SQLite.
    """
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    for name, table, _ in REDUNDANT_UNIQUE_CONSTRAINTS:
        # IF EXISTS because a database built by create_all never had these —
        # this revision has to be a no-op there, not an error.
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{name}"')

    index_name, _, _ = UNUSABLE_INDEX
    op.execute(f'DROP INDEX IF EXISTS "{index_name}"')


def downgrade() -> None:
    if not _is_postgres():
        return

    index_name, index_table, index_column = UNUSABLE_INDEX
    op.create_index(index_name, index_table, [index_column], if_not_exists=True)

    for name, table, column in REDUNDANT_UNIQUE_CONSTRAINTS:
        # Re-adding is only possible while the data still satisfies it, which
        # it must: the ix_* unique index has been enforcing the same rule the
        # entire time this revision was applied.
        op.execute(
            f'ALTER TABLE {table} ADD CONSTRAINT "{name}" UNIQUE ({column})'
        )
