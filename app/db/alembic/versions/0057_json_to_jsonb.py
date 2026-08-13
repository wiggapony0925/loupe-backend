"""Convert the ``JsonCol`` columns from ``json`` to ``jsonb``.

``app/db/types.py`` claimed "JsonCol maps to JSONB on Postgres" and it
never did — ``sqlalchemy.JSON`` compiles to ``json`` on the postgres
dialect, so all twenty JSON columns on the server were plain ``json``.
That is not a cosmetic difference: ``json`` keeps the document as the text
it arrived as, which means every read reparses it, none of the containment
operators (``@>``, ``?``, ``@@``) are defined for it, and it cannot carry a
GIN index. Nothing queries these columns that way today — which is exactly
why the wrong comment survived, and exactly the trap this closes: the first
person to write the ``@>`` filter the comment promised would have got a
sequential scan with a per-row cast, or a syntax error.

SCOPE — fifteen columns, not twenty. ``JsonCol`` is now a variant type that
renders JSONB on postgres and JSON on SQLite, so the fifteen columns
declared through it are jsonb after this runs. Five columns spell
``sqlalchemy.JSON`` inline instead (``users.mfa_backup_codes``,
``notifications.data``, ``ai_search_log.candidates``,
``ai_search_log.results``, ``email_log.headers``) and are deliberately left
as ``json`` here. Converting them would put the migrated schema out of step
with ``Base.metadata`` — and metadata is not a footnote in this repo, it is
how every local and CI database is actually built (``0001_initial`` is
``create_all`` in a trench coat; see tests/database/test_migrations.py). A
production database with jsonb and a developer database with json is the
precise failure the tests/database suite exists to prevent. Those five want
their models switched to ``JsonCol`` and a migration of their own.

WHAT THIS COSTS IN PRODUCTION — read before running it there.
``ALTER TABLE ... ALTER COLUMN ... TYPE ... USING`` rewrites the entire
table and rebuilds every index on it, holding an ACCESS EXCLUSIVE lock the
whole time: no reads, no writes, not even a ``SELECT``. There is no
``CONCURRENTLY`` form of ``ALTER COLUMN TYPE`` — that keyword exists for
``CREATE INDEX`` and ``REFRESH MATERIALIZED VIEW``, and nothing here can
borrow it. It also needs free disk for a second full copy of the largest
table plus its indexes while the rewrite runs.

On the dev database this is free and was measured: the whole database is
~12 MB, the largest table carrying a converted column is
``catalog_mirror_cards`` at 96 kB, thirteen of the fifteen tables are
empty, and the upgrade completes in milliseconds. Do not read that as
"cheap". The two tables that grow without bound are ``catalog_mirror_cards``
(one full upstream card document per row) and ``cards`` — at a few million
rows and a few GB that rewrite is minutes, not milliseconds. And
``env.py`` wraps the entire upgrade in ONE transaction, so the fifteen
ACCESS EXCLUSIVE locks are all held until the last rewrite commits: the
outage is the sum of the rewrites, not the longest one.

So on a production-sized database: take a maintenance window, or do not run
this migration at all and use the online pattern instead — add a jsonb
column beside each json one, backfill in bounded batches, keep both in sync
with a trigger, then take a short lock only to swap the names. Storage is
not the reason to do either: jsonb drops insignificant whitespace and
duplicate keys but adds per-key offsets, so expect the size to land within
noise of where it started.

IDEMPOTENT ON PURPOSE. Each column is checked against the catalog and
skipped if it is already the target type, for two reasons that both really
happen here. First, ``0009_card_identifications`` and
``0036_pricecharting_prices`` import ``JsonCol`` from the live module, so
replaying the chain now creates those two columns as jsonb directly and
this migration must not treat that as an error. Second, a database built by
``create_all`` and stamped mid-chain (the documented workaround for the
broken chain) already has every one of these as jsonb.

Revision ID: 0057_json_to_jsonb
Revises: 0056_fk_indexes_and_guards
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0057_json_to_jsonb"
down_revision: str | Sequence[str] | None = "0056_fk_indexes_and_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The columns declared with ``app.db.types.JsonCol``. Written out rather
#: than derived from ``Base.metadata`` at runtime: a migration has to keep
#: describing the change it made on the day it was written, and a list read
#: from today's models would silently start converting columns that did not
#: exist when this ran.
JSON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("audit_log", "payload"),
    ("card_identifications", "candidates_json"),
    ("cards", "metadata"),
    ("catalog_mirror_cards", "payload"),
    ("catalog_mirror_sets", "payload"),
    ("fingerprints", "feature_vector"),
    ("graded_cards", "subgrades"),
    ("graded_cards", "tags"),
    ("price_snapshots", "raw_payload"),
    ("pricecharting_prices", "ladder"),
    ("scan_jobs", "images_s3_keys"),
    ("sealed_products", "price_history"),
    ("social_profiles", "links"),
    ("user_recents", "searches"),
    ("user_recents", "viewed"),
)


def _columns_currently(data_type: str) -> set[tuple[str, str]]:
    """The (table, column) pairs postgres currently stores as ``data_type``.

    One catalog query for all fifteen rather than fifteen round trips —
    and it answers for the schema the connection is actually pointed at,
    so a migration run against a scratch database cannot accidentally read
    another one's types.
    """
    rows = op.get_bind().execute(
        sa.text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND data_type = :t"
        ),
        {"t": data_type},
    )
    return {(str(table), str(column)) for table, column in rows}


def _retype(target: str, from_type: str) -> None:
    """Convert every column still stored as ``from_type`` over to ``target``.

    ``USING col::target`` converts in place — postgres parses the text on
    the way to jsonb and reserialises it on the way back — so no data is
    staged anywhere and NULL stays NULL. It is still a full table rewrite;
    see the module docstring for what that costs at scale.
    """
    if op.get_bind().dialect.name != "postgresql":
        # json and jsonb are the same thing to SQLite, which stores both as
        # TEXT, and information_schema does not exist there to ask. Nothing
        # runs this chain on SQLite today; the guard is so that the day
        # someone tries, they get a no-op instead of an OperationalError
        # that reads like a broken migration.
        return

    pending = _columns_currently(from_type)
    for table, column in JSON_COLUMNS:
        if (table, column) not in pending:
            continue
        op.alter_column(
            table,
            column,
            type_=postgresql.JSONB() if target == "jsonb" else postgresql.JSON(),
            postgresql_using=f'"{column}"::{target}',
        )


def upgrade() -> None:
    _retype("jsonb", from_type="json")


def downgrade() -> None:
    """jsonb back to json, losslessly enough to be worth having.

    The round trip is not byte-for-byte: a document that went in with keys
    in a particular order, insignificant whitespace, or duplicate keys comes
    back normalised, because jsonb threw that away on the way in. Every
    payload here is machine-generated and consumed as a dict, so the
    downgrade restores the type and the data, just not the formatting. It is
    the same full table rewrite in reverse.
    """
    _retype("json", from_type="jsonb")
