"""Index the unindexed foreign keys; close the sets the models only documented.

Four things, all of them gaps rather than features.

1. SEVEN foreign keys had no index leading with their column. Postgres builds
   one for a primary key and for a unique constraint and never for a foreign
   key, so an unindexed FK costs twice: every lookup THROUGH it reads the whole
   child table, and so does every DELETE of the PARENT row, because postgres
   has to go looking for referencing rows. Two of them are ON DELETE CASCADE,
   which is the sharp end — `portfolio_snapshots.collection_id` meant deleting
   one binder scanned a table that grows ~48 rows/user/day forever.

2. FIVE indexes existed in this database and in NO model. They were created by
   raw DDL in 0037 and 0040, so they were present in a migrated database and
   absent from any built with `create_all` — the tests, and any fresh local
   bootstrap. That is exactly the drift point 1 would have re-created, so every
   index this revision adds is declared in its model as well, and the four that
   0040 owns are back-ported there too. (The fifth, `ix_card_embeddings_cosine`,
   is an ivfflat index on `catalog_card_embeddings`, which has no ORM model at
   all — the ORM has no pgvector type. Left alone here.)

3. FIVE columns the models describe as closed sets ("open" | "dismissed" |
   "removed", 'free' | 'pro', a 0-10 grade) that postgres would take anything
   for. A typo in one of those does not fail; it writes a row no query in the
   app will ever match again.

4. SIX NOT NULL columns on `users` with a Python-side default and no server
   default, so any INSERT that does not go through the ORM — a psql backfill, a
   data-repair script, a restore — died with a bare 23502.

VERIFIED AGAINST THE LIVE DATA BEFORE WRITING THIS. env.py runs the whole
chain in ONE transaction, so a CHECK that any existing row violates does not
fail alone — it rolls back every revision in the upgrade. On the dev database
at 0055: `graded_cards` and `social_moderation_cases` were empty, and all five
`users` rows carried plan = 'free'. Re-check with SELECT DISTINCT before this
reaches an environment with real data.

IDEMPOTENT ON PURPOSE. Nine of the ten indexes below already exist somewhere:
four in every migrated database (0040), and all of them in any database built
from today's models. `IF NOT EXISTS` makes this revision a no-op on those
rather than a DuplicateTable error.

LOCKING. Plain `CREATE INDEX` and `ALTER TABLE ... ADD CONSTRAINT`, both of
which lock the table for the duration. Neither `CONCURRENTLY` nor a `NOT VALID`
/ `VALIDATE` split buys anything here, because both need to run outside a
transaction and env.py wraps the migration in one. On tables this size that is
a blip; before this runs against a large production `graded_cards`, split the
CHECK into its own out-of-transaction step.

Revision ID: 0056_fk_indexes_and_guards
Revises: 0055_social_links
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056_fk_indexes_and_guards"
down_revision = "0055_social_links"
branch_labels = None
depends_on = None


#: The foreign keys this revision indexes for the first time — (index, table,
#: columns, why it hurt). These are the ones the downgrade removes, because
#: they did not exist before it.
NEW_FK_INDEXES: list[tuple[str, str, list[str]]] = [
    # SET NULL. Deleting an admin account scanned every event row.
    (
        "ix_application_events_created_by_user_id",
        "application_events",
        ["created_by_user_id"],
    ),
    # CASCADE, and the urgent one: snapshots grow per user per day forever,
    # so "delete a binder" got slower the longer the product had been running.
    ("ix_portfolio_snapshots_collection_id", "portfolio_snapshots", ["collection_id"]),
    # Three SET NULL keys into `users` on one table: deleting one account read
    # the whole case table three times over.
    (
        "ix_social_moderation_cases_reporter_id",
        "social_moderation_cases",
        ["reporter_id"],
    ),
    ("ix_social_moderation_cases_author_id", "social_moderation_cases", ["author_id"]),
    (
        "ix_social_moderation_cases_resolved_by_id",
        "social_moderation_cases",
        ["resolved_by_id"],
    ),
    # SET NULL, and the query behind a card page's community section.
    ("ix_social_posts_card_id", "social_posts", ["card_id"]),
]

#: Indexes 0040 created directly in postgres and no model declared. Created
#: here only for databases that lack them, and NOT dropped on downgrade —
#: 0040 owns them, and a `downgrade -1` that removed them would leave the
#: database missing indexes it had before this revision ever ran.
#:
#: `ix_collection_items_graded_card_id` is in both lists' territory: it is the
#: index the seventh unindexed foreign key needs AND one of the five 0040
#: created. It is listed here because 0040 is the revision that owns it.
BACKPORTED_0040_INDEXES: list[tuple[str, str, list[str]]] = [
    ("ix_collection_items_graded_card_id", "collection_items", ["graded_card_id"]),
    ("ix_graded_cards_user_active", "graded_cards", ["user_id", "deleted_at"]),
    ("ix_graded_cards_user_value", "graded_cards", ["user_id", "estimated_value_usd"]),
    ("ix_graded_cards_user_grade", "graded_cards", ["user_id", "grade"]),
]

#: (constraint, table, condition). The names are the ones `create_all` renders,
#: passed through `op.f()` so alembic takes them verbatim: Base's naming
#: convention is "ck_%(table_name)s_%(constraint_name)s", which wraps the name
#: the model gives — `ck_user_plan` in the model is `ck_users_ck_user_plan` in
#: the database. Spelling the final name here is what keeps a migrated database
#: and a create_all-built one carrying the same constraint.
CHECKS: list[tuple[str, str, str]] = [
    # 0-10 matches the API contract (`GradedCardCreate.grade` is ge=0/le=10),
    # not any one house's scale: every house we recognise tops out at 10, and
    # 0 is the sentinel a RAW holding carries. Grade drives the price lookup
    # and therefore the portfolio total, so a 99.9 from a broken import is a
    # wrong number on a user's dashboard, not a cosmetic blemish.
    (
        "ck_graded_cards_ck_graded_card_grade",
        "graded_cards",
        "grade >= 0 AND grade <= 10",
    ),
    (
        "ck_users_ck_user_plan",
        "users",
        "plan IN ('free', 'pro')",
    ),
    # "blocked" is absent deliberately — the queue's blocked tab is a filter
    # over (removed, source=auto, unresolved), never a stored value.
    (
        "ck_social_moderation_cases_ck_moderation_case_status",
        "social_moderation_cases",
        "status IN ('open', 'dismissed', 'removed')",
    ),
    # All seven surfaces that call safety.enforce(), not the three a user may
    # report — the other four arrive as classifier auto-cases.
    (
        "ck_social_moderation_cases_ck_moderation_case_target_type",
        "social_moderation_cases",
        (
            "target_type IN ('post', 'comment', 'profile', 'review', "
            "'collection', 'story', 'story_comment')"
        ),
    ),
    (
        "ck_social_moderation_cases_ck_moderation_case_source",
        "social_moderation_cases",
        "source IN ('auto', 'report')",
    ),
]

#: (column, type, server default). The Python-side defaults on the model stay
#: exactly as they were, so ORM behaviour does not change; these are for the
#: writers that are not the ORM.
USER_SERVER_DEFAULTS: list[
    tuple[str, sa.types.TypeEngine, sa.sql.elements.ClauseElement]
] = [
    ("is_admin", sa.Boolean(), sa.false()),
    ("failed_login_count", sa.Integer(), sa.text("0")),
    ("token_version", sa.Integer(), sa.text("0")),
    ("mfa_enabled", sa.Boolean(), sa.false()),
    ("plan", sa.String(16), sa.text("'free'")),
    ("pro_trialing", sa.Boolean(), sa.false()),
]


def _is_postgres() -> bool:
    """True on the engine this revision is written for.

    The CHECK constraints and server defaults below are postgres DDL: SQLite
    cannot ADD CONSTRAINT or change a server default without rebuilding the
    table, and the catalog probe in `_has_constraint` has no meaning off
    postgres. Every deployment is postgres, so this guard costs nothing there.

    The one non-postgres caller is the migration replay in
    tests/social/test_social_feed.py, which runs on SQLite and asserts columns
    and indexes — both created unguarded above, so guarding here does not
    weaken what that test checks.
    """
    return op.get_bind().dialect.name == "postgresql"


def _has_constraint(name: str, table: str) -> bool:
    """True when `table` already carries a constraint called `name`.

    Postgres has no ``ADD CONSTRAINT IF NOT EXISTS``, and this revision has to
    be a no-op on a database built from today's models (where create_all
    already added all five). Probing the catalog is the only way to get the
    same idempotence indexes get for free.
    """
    return bool(
        op.get_bind().scalar(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = :name AND conrelid = to_regclass(:table)"
            ),
            {"name": name, "table": table},
        )
    )


def upgrade() -> None:
    for name, table, columns in NEW_FK_INDEXES + BACKPORTED_0040_INDEXES:
        op.create_index(name, table, columns, unique=False, if_not_exists=True)

    if not _is_postgres():
        return

    for name, table, condition in CHECKS:
        if not _has_constraint(name, table):
            op.create_check_constraint(op.f(name), table, condition)

    for column, type_, default in USER_SERVER_DEFAULTS:
        op.alter_column(
            "users",
            column,
            existing_type=type_,
            existing_nullable=False,
            server_default=default,
        )


def downgrade() -> None:
    if _is_postgres():
        for column, type_, _ in USER_SERVER_DEFAULTS:
            op.alter_column(
                "users",
                column,
                existing_type=type_,
                existing_nullable=False,
                server_default=None,
            )

        for name, table, _ in CHECKS:
            if _has_constraint(name, table):
                op.drop_constraint(op.f(name), table, type_="check")

    # Only the indexes this revision introduced. The four in
    # BACKPORTED_0040_INDEXES are left standing on purpose — see the note on
    # that list.
    for name, table, _ in NEW_FK_INDEXES:
        op.drop_index(name, table_name=table, if_exists=True)
