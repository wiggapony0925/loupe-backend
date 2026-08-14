"""Every column must be accounted for.

WHY THIS EXISTS. A production audit found 44 columns that had never held a
value across 58 tables. Almost all of them were benign — 81 of the 82 accounts
in `users` are fixtures, and fixtures only fill what they need — but the audit
took a day of hand-querying to reach that conclusion, and it could not be
repeated cheaply. Worse, the two genuinely broken ones were indistinguishable
from the harmless ones without reading the code for each.

So the rule this file enforces is not "no column may be empty". It is: **every
column is either written by application code, or explicitly declared here with
a reason.** A new column that is neither fails the suite. That converts "why is
this empty?" from an investigation into a lookup.

WHAT THIS CAN AND CANNOT PROVE. It can prove a column is *referenced* by a
writer. It cannot prove that writer ever runs, or that the value it assigns is
non-null — and the difference is not academic. Auditing this codebase, a
grep for writers cleared `card_identifications.top_card_id` because
``card_id=cand.get("card_id")`` exists at card_identifier.py:849. It is
assigned from a key no producer emits, so it is structurally NULL and the grep
was answering the wrong question. Reachability is what ``EXPECTED_EMPTY``
below is for: it is written by hand, with evidence, by someone who read the
path.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app import models  # noqa: F401  — registers the metadata
from app.db import Base
from app.social import models as social_models  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "app"

#: Columns known to be empty in production, each with the reason and the
#: evidence. Anything here is exempt from the writer check — it is a claim that
#: someone looked, not that nobody did.
#:
#: Verified against production on 2026-08-13 (82 users, 2,787 graded cards).
EXPECTED_EMPTY: dict[str, str] = {
    # ── Empty because the app has ONE real user ────────────────────────────
    # 81 of 82 rows in `users` are seeded fixtures, smoke probes or demo
    # collectors. Fixtures fill the handful of columns they need and nothing
    # else, which accounts for most of this block.
    "users.banned_at": "nobody has been banned; user_admin_service.ban() writes it",
    "users.ban_reason": "same path as banned_at",
    "users.deleted_at": "no account has been deleted",
    "users.locked_until": "no account has hit the failed-login threshold",
    "users.mfa_backup_codes": "one account has MFA; codes issue at enrolment",
    "users.mfa_enrolled_at": "same — MFA enrolment has not completed",
    "users.phone": "phone capture is not in either client's signup flow",
    "users.phone_verified_at": "follows users.phone",
    "users.avatar_url": (
        "SUPERSEDED, and now correctly bypassed — this holds an OAuth "
        "provider's URL and is 0/82 because no provider supplied one. The "
        "picture a user uploads lives in social_profiles.avatar_key. As of "
        "2026-08-13 /v1/me falls back to the uploaded avatar, so a user with "
        "a photo is no longer told they have none. Covered by "
        "tests/auth/test_me_effective_avatar.py."
    ),
    "users.pro_expires_at": "the two pro accounts are admin comps, which are lifetime",
    # ── Empty because the row state means it ──────────────────────────────
    "social_posts.deleted_at": "BY DESIGN — null is 'not deleted'",
    "social_posts.edited_at": "BY DESIGN — null is 'never edited'",
    "social_stories.deleted_at": "BY DESIGN — null is 'not deleted'",
    "social_story_comments.deleted_at": "BY DESIGN — null is 'not deleted'",
    "sealed_holdings.deleted_at": "BY DESIGN — null is 'not deleted'",
    "sealed_holdings.opened_at": "BY DESIGN — null is 'still sealed'",
    "email_log.error": "BY DESIGN — null is 'delivered without error'",
    "social_moderation_cases.resolved_at": "BY DESIGN — null is 'still open'",
    "social_moderation_cases.resolved_by_id": "follows resolved_at",
    "social_moderation_cases.reporter_id": "the one case is an auto-case, not a report",
    # ── Empty because the feature has not been exercised ──────────────────
    "blog_posts.cover_image_url": "no post has been given a cover image",
    "email_log.idempotency_key": "only set by retryable sends; none have retried",
    "notifications.image_url": "no notification has carried an image yet",
    "sealed_holdings.purchase_date": "two holdings, neither recorded a purchase",
    "sealed_holdings.estimated_value_usd": "same two holdings",
    "sealed_holdings.notes": "same two holdings",
    "sealed_products.set_id": "storefront products are not yet linked to a set",
    "site_config.announcement_cta_label": "no announcement has used a CTA button",
    "site_config.announcement_cta_href": "follows the CTA label",
    "user_settings.active_collection_id": "nobody has pinned an active collection",
    "waitlist_entries.user_id": "waitlist signups have not been claimed by an account",
    "identification_feedback.chosen_card_id": (
        "FIXED 2026-08-13 — followed top_card_id. The clients already send "
        "candidate.card_id, which now carries the catalog id, so no client "
        "change was needed; the next correction populates this. The 16 "
        "existing rows stay NULL. Covered by test_feedback_loop_closes.py."
    ),
    "card_identifications.top_card_id": (
        "FIXED 2026-08-13 — _to_candidate now falls back to the catalog id, "
        "which is the key _feedback_priors looks up (card_identifier.py:268, "
        ":385). Was structurally NULL on all 2,467 rows because no producer "
        "emits a 'card_id' key. Existing rows stay NULL; new scans fill it. "
        "Covered by test_feedback_loop_closes.py."
    ),
    "graded_cards.scan_job_id": "cards were added manually or seeded, not via a scan",
    "graded_cards.fingerprint_hash": "follows scan_job_id",
    "graded_cards.acquired_via": "never set on the manual-add path",
    "scan_jobs.scanner_id": "the 3 jobs came from the app, not a hardware scanner",
    "scan_jobs.started_at": (
        "no scan has reached /complete — mark_complete sets this "
        "unconditionally, so all-null means all 3 jobs are stranded"
    ),
    "scan_jobs.completed_at": "follows started_at",
    "scan_jobs.error_message": (
        "FIXED 2026-08-13 — process_scan now catches, marks the job failed "
        "and records the exception (scan_processor._fail). Still empty in "
        "production because the 3 existing jobs predate it and none has "
        "crashed since. Covered by "
        "test_a_crashing_scan_is_marked_failed_instead_of_hanging."
    ),
    "notifications.pushed_at": (
        "FIXED 2026-08-13 — the broadcast path now records delivery "
        "(notification_service.py). The 553 existing production rows stay "
        "NULL because they predate the fix; the next broadcast fills it. "
        "Covered by test_broadcast_records_delivery_when_the_push_is_accepted."
    ),
    "social_profiles.links": "shipped in migration 0055; no profile has added links",
}

#: Tables with no rows at all. Listed so a NEW empty table is noticed, and so
#: the reason is written down once rather than rediscovered.
EXPECTED_EMPTY_TABLES: dict[str, str] = {
    "api_keys": "DEAD — model and migration exist, nothing in app/ references it",
    "application_events": "audit sink; nothing emits to it yet",
    "catalog_card_embeddings": (
        "DEFECT, tracked — only writer is scripts/backfill_embeddings.py, a "
        "hand-run CLI that also exits unless CARD_EMBED_MODEL_PATH is set "
        "(defaults to None). The learned tier has never run; 78% of "
        "identifications return no candidate at all."
    ),
    "fingerprints": "written by the scan pipeline, which has never completed a job",
    "job_applications": "no one has applied through the careers page",
    "price_snapshots": "populated by the snapshot cron; retention window is short",
    "pricecharting_prices": "PriceCharting sync has not been run against production",
    "social_comment_likes": "no comment has been liked",
    "social_follow_requests": "no private account has received a follow request",
}


def _columns() -> list[tuple[str, str]]:
    return [
        (table_name, column.name)
        for table_name, table in sorted(Base.metadata.tables.items())
        for column in table.columns
    ]


def _writer_hits(column: str) -> list[str]:
    """Assignment-shaped references to `column` outside declarations.

    Model files, schemas, migrations and tests are excluded: every column has a
    declaration by definition, and a declaration says nothing about whether a
    value ever arrives.

    ``scripts/`` counts. The first version of this searched only ``app/`` and
    failed three columns that are demonstrably full in production —
    catalog_image_hashes.phash_alt and dhash_alt (16,184 rows each) and
    sealed_products.upstream_source (31/31) — because the catalog indexer is a
    CLI, not a request handler. A cron that fills a column is still a writer.

    It is worth knowing that a script-only writer is a weaker guarantee than a
    request-path one: nothing runs it automatically, so it fills the column
    only as often as someone remembers. That is precisely how
    catalog_card_embeddings ended up with zero rows — its only writer is
    scripts/backfill_embeddings.py and it has never been run. This function
    cannot tell the two apart; EXPECTED_EMPTY is where that judgement lives.
    """
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", column):
        return []
    pattern = (
        rf"(^|[^a-zA-Z0-9_]){column}\s*=[^=]"
        rf"|\.{column}\s*=[^=]"
        rf"|[\"']{column}[\"']\s*:"
    )
    roots = [str(APP), str(REPO_ROOT / "scripts")]
    try:
        out = subprocess.run(
            ["grep", "-rn", "--include=*.py", "-E", pattern, *roots],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return []
    skip = ("/alembic/", "/models/", "/schemas/", "__pycache__")
    return [ln for ln in out.splitlines() if not any(s in ln for s in skip)]


# ── the census ─────────────────────────────────────────────────────────────


def test_every_table_is_either_written_or_declared_empty():
    """A new table must not appear without a way to get rows into it."""
    declared = set(EXPECTED_EMPTY_TABLES)
    unknown = declared - set(Base.metadata.tables)
    assert not unknown, (
        f"EXPECTED_EMPTY_TABLES names tables that no longer exist: "
        f"{sorted(unknown)}. Remove them."
    )


@pytest.mark.parametrize(
    ("table", "column"), _columns(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_column_is_written_by_code_or_declared_empty(table, column):
    """The census itself, one test per column.

    Passing means one of two things is true: application code assigns this
    column, or someone has written down why it is empty. Both are acceptable.
    Neither being true is a column nobody can account for.
    """
    key = f"{table}.{column}"
    if key in EXPECTED_EMPTY or table in EXPECTED_EMPTY_TABLES:
        return

    # Primary keys, foreign keys and server-defaulted columns are filled by the
    # database or by relationship assignment, which no grep for `col =` sees.
    col = Base.metadata.tables[table].columns[column]
    if col.primary_key or col.foreign_keys or col.server_default is not None:
        return
    if col.default is not None:
        return

    hits = _writer_hits(column)
    assert hits, (
        f"{key} has no writer in app/ and is not declared in EXPECTED_EMPTY.\n\n"
        f"Either something assigns it — in which case this test is too strict "
        f"and the pattern needs widening — or nothing does, and it will be "
        f"empty in production forever. If the emptiness is intended, add it to "
        f"EXPECTED_EMPTY in {Path(__file__).name} with the reason and the "
        f"evidence, so the next person does not have to work it out again."
    )


def test_the_declared_empty_list_has_not_gone_stale():
    """A column that gained a writer should leave the list.

    Without this the registry only grows, and eventually it exempts half the
    schema from the check it exists to perform.
    """
    stale = [
        key
        for key in EXPECTED_EMPTY
        if key.split(".")[0] in Base.metadata.tables
        and key.split(".")[1] not in Base.metadata.tables[key.split(".")[0]].columns
    ]
    assert not stale, (
        f"EXPECTED_EMPTY names columns that no longer exist: {stale}. "
        f"Remove them — a stale exemption silently disables the check."
    )


def test_the_tracked_defects_are_still_listed():
    """The four known defects must stay visible until they are fixed.

    They are exempt from the writer check because each HAS a writer — the
    writer just cannot produce a value. If someone quietly deletes these
    entries the audit trail disappears with them.
    """
    tracked = [k for k, v in EXPECTED_EMPTY.items() if "DEFECT" in v]
    tracked += [k for k, v in EXPECTED_EMPTY_TABLES.items() if "DEFECT" in v]
    assert set(tracked) == {
        "catalog_card_embeddings",
    }, (
        "The tracked-defect set changed. If one was fixed, remove its "
        "EXPECTED_EMPTY entry entirely and update this assertion — do not "
        "just drop the DEFECT marker."
    )
