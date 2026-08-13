"""Does the database actually REFUSE bad data?

WHY THIS FILE EXISTS. Every constraint in ``app/models`` is a promise —
"an account has exactly one email", "a like happens once", "a store review
is one to five stars". Until now nothing checked that postgres keeps those
promises, because the entire suite runs on SQLite, which is a far more
forgiving database: it takes any string in a ``VARCHAR(30)``, and its
foreign keys are OFF unless a connection turns them on. A schema can look
airtight in 2,089 green tests and still be a schema in which anyone can
claim a taken username.

So each test here does the one thing no other test in the repo does: it
tries to write a row that must not exist, and asserts postgres said no —
with the SQLSTATE, because *which* rule fired is the whole point. A unique
violation where a not-null violation was expected means the row was wrong
for a reason nobody predicted.

The second half is the other direction: constraints that are supposed to
CHANGE data rather than refuse it. ON DELETE CASCADE must really reach the
grandchildren, and ON DELETE SET NULL must really leave the row standing —
those two differ by a single word in the model and by everything in what
the user sees afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card
from app.models.collection import Collection, CollectionItem
from app.models.grade import GradedCard
from app.models.user import User
from app.social.models import (
    SocialFollow,
    SocialFollowRequest,
    SocialModerationCase,
    SocialPost,
    SocialPostComment,
    SocialPostLike,
    SocialPostMedia,
    SocialProfile,
    SocialProfileLike,
    StoreReview,
)
from tests.factories import make_card, make_user

# Postgres class-23 integrity codes. Asserting these rather than merely
# "something raised" is what makes a failure diagnosable: the test says
# which promise it expected the database to keep.
NOT_NULL = "23502"
FOREIGN_KEY = "23503"
UNIQUE = "23505"
CHECK = "23514"

# Where a test names the constraint that fired, it names what is actually IN
# the database, which is not always what the model says. Two of Base's naming
# conventions (app/db/base.py) rewrite the name on the way in:
#
#   * "ck_%(table_name)s_%(constraint_name)s" wraps the model's own name, so
#     `ck_store_review_rating` is stored as
#     `ck_store_reviews_ck_store_review_rating` — the doubled `ck_` below is
#     real, not a typo.
#   * `unique=True, index=True` on a column is a unique INDEX, not a UNIQUE
#     constraint, so users.email reports as `ix_users_email`. Worth knowing
#     before writing `ON CONFLICT ON CONSTRAINT` against one of them.


def _sqlstate(exc: IntegrityError) -> str | None:
    """The five-character postgres code inside SQLAlchemy's wrapper."""
    return getattr(exc.orig, "sqlstate", None)


def _constraint_name(exc: IntegrityError) -> str | None:
    """Which named constraint fired (asyncpg reports it; SQLite cannot).

    Two unwraps deep: SQLAlchemy's ``IntegrityError`` wraps the asyncpg
    dialect's DBAPI-shaped stand-in (``exc.orig``, which carries only the
    sqlstate), and that one is raised ``from`` the real asyncpg error, which
    is the only object that knows the constraint's name.
    """
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)


@asynccontextmanager
async def rejects(
    session: AsyncSession, sqlstate: str, *, constraint: str | None = None
) -> AsyncIterator[None]:
    """Assert the statements inside are refused with ``sqlstate``.

    Runs them inside a SAVEPOINT. Postgres aborts the whole transaction the
    instant a constraint fires, so without one a test could make exactly one
    assertion and then be dead in the water — and half the value here is in
    showing the *neighbouring* write still succeeds.
    """
    savepoint = await session.begin_nested()
    try:
        yield
    except IntegrityError as exc:
        assert _sqlstate(exc) == sqlstate, (
            f"expected SQLSTATE {sqlstate}, got {_sqlstate(exc)} "
            f"from constraint {_constraint_name(exc)!r}"
        )
        if constraint is not None:
            assert _constraint_name(exc) == constraint
    else:
        pytest.fail(f"expected SQLSTATE {sqlstate}, but the database accepted the row")
    finally:
        if savepoint.is_active:
            await savepoint.rollback()


async def _post(
    session: AsyncSession, author: User, *, card_id: uuid.UUID | None = None
) -> uuid.UUID:
    """Insert one feed post, returning its id."""
    post_id = uuid.uuid4()
    await session.execute(
        insert(SocialPost).values(
            id=post_id, author_id=author.id, body="mint copy", card_id=card_id
        )
    )
    return post_id


async def _graded_card(session: AsyncSession, owner: User, card: Card) -> uuid.UUID:
    """Insert one holding (a user's copy of a catalog card)."""
    graded_id = uuid.uuid4()
    await session.execute(
        insert(GradedCard).values(
            id=graded_id,
            user_id=owner.id,
            card_id=card.id,
            grade=Decimal("9.5"),
        )
    )
    return graded_id


async def _collection(session: AsyncSession, owner: User) -> uuid.UUID:
    collection_id = uuid.uuid4()
    await session.execute(
        insert(Collection).values(id=collection_id, user_id=owner.id, name="Binder")
    )
    return collection_id


async def _count(session: AsyncSession, model, where) -> int:
    """Rows of ``model`` matching ``where`` — read through the same session,
    so it sees this test's uncommitted writes and nothing from any other."""
    return await session.scalar(select(func.count()).select_from(model).where(where))


# ── NOT NULL ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_account_cannot_exist_without_an_email(pg_session):
    """Email is the account's identity: it is what sign-in resolves, what
    password reset and every transactional notice are sent to, and the key
    the ADMIN_EMAILS allowlist matches. A row without one is an account
    nobody can ever reach or log into again."""
    async with rejects(pg_session, NOT_NULL):
        # Omitted entirely, the way a raw backfill or a hand-written INSERT
        # would omit it — not passed as an explicit None.
        await pg_session.execute(insert(User).values(display_name="No Address"))


@pytest.mark.asyncio
async def test_a_post_cannot_exist_without_an_author(pg_session):
    """An unattributable post is unmoderatable: nobody can be asked to take
    it down, no report can name a culprit, and the account-deletion cascade
    has nothing to hang it on, so it would outlive every account forever."""
    async with rejects(pg_session, NOT_NULL):
        await pg_session.execute(insert(SocialPost).values(body="from nobody"))


@pytest.mark.asyncio
async def test_a_collection_cannot_exist_without_an_owner(pg_session):
    """An ownerless binder is invisible (every read scopes by user_id) and
    immortal (the users → collections cascade never reaches it) — a row that
    accrues storage and can never be seen or deleted."""
    async with rejects(pg_session, NOT_NULL):
        await pg_session.execute(insert(Collection).values(name="Orphan binder"))


@pytest.mark.asyncio
async def test_a_comment_cannot_have_an_empty_body(pg_session):
    """A comment is only its text. A NULL one still occupies a slot in the
    thread and still counts toward the reply count, so the UI shows a blank
    row under a post that nobody can explain or reply to sensibly."""
    user = await make_user(pg_session)
    post_id = await _post(pg_session, user)

    async with rejects(pg_session, NOT_NULL):
        await pg_session.execute(
            insert(SocialPostComment).values(post_id=post_id, author_id=user.id)
        )


# ── UNIQUE ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_accounts_cannot_share_an_email(pg_session):
    """Sign-in looks an account up BY email. Two rows with the same address
    make that lookup non-deterministic — the same person signing in twice
    could land in two different vaults."""
    first = await make_user(pg_session, email="collector@example.com")

    async with rejects(pg_session, UNIQUE, constraint="ix_users_email"):
        await pg_session.execute(
            insert(User).values(email=first.email, display_name="Impostor")
        )


@pytest.mark.asyncio
async def test_two_accounts_cannot_share_an_apple_identity(pg_session):
    """The Apple subject is the stable id Apple hands back for one person.
    If two rows could carry it, "sign in with Apple" would be an account
    takeover primitive: create a row with someone else's subject and their
    next sign-in is a coin flip."""
    first = await make_user(pg_session)

    async with rejects(pg_session, UNIQUE):
        await pg_session.execute(
            insert(User).values(
                email="second@example.com", apple_subject=first.apple_subject
            )
        )


@pytest.mark.asyncio
async def test_two_people_cannot_claim_the_same_username(pg_session):
    """Handles are how the whole social layer addresses people: profile URLs,
    @mentions, and the follow endpoints all resolve a username to exactly one
    account. Two owners means mentions notify the wrong collector."""
    first = await make_user(pg_session)
    second = await make_user(pg_session)
    await pg_session.execute(
        insert(SocialProfile).values(user_id=first.id, username="mintcondition")
    )

    async with rejects(pg_session, UNIQUE, constraint="ix_social_profiles_username"):
        await pg_session.execute(
            insert(SocialProfile).values(user_id=second.id, username="mintcondition")
        )


@pytest.mark.asyncio
async def test_a_unique_column_still_lets_many_rows_leave_it_empty(pg_session):
    """The other half of "unique": postgres treats NULLs in a unique index as
    distinct from one another, so ``phone`` can be both UNIQUE and optional.
    Without that rule the second account that never supplied a number would
    be unfixably rejected — and the alternative (a sentinel empty string)
    would collide for exactly the same reason.

    The violation is raised by an UPDATE rather than an INSERT, which is the
    path a "change my number" endpoint actually takes."""
    first = await make_user(pg_session)
    second = await make_user(pg_session)
    assert first.phone is None and second.phone is None  # both stored happily

    await pg_session.execute(
        update(User).where(User.id == first.id).values(phone="+14155550123")
    )

    async with rejects(pg_session, UNIQUE, constraint="ix_users_phone"):
        await pg_session.execute(
            update(User).where(User.id == second.id).values(phone="+14155550123")
        )


@pytest.mark.asyncio
async def test_a_person_can_only_like_a_post_once(pg_session):
    """The like count is a COUNT over these rows — there is no counter column
    to drift. That honesty depends entirely on (user, post) being unique;
    without it a double-tap, or a retried request, inflates the number."""
    author = await make_user(pg_session)
    liker = await make_user(pg_session)
    post_id = await _post(pg_session, author)
    await pg_session.execute(
        insert(SocialPostLike).values(user_id=liker.id, post_id=post_id)
    )

    async with rejects(pg_session, UNIQUE):
        await pg_session.execute(
            insert(SocialPostLike).values(user_id=liker.id, post_id=post_id)
        )


@pytest.mark.asyncio
async def test_a_person_can_only_like_a_profile_once(pg_session):
    """Same rule one table over, and worth its own test because the composite
    key is spelled differently (liker → profile_user): a profile's like count
    must be people, not taps."""
    liker = await make_user(pg_session)
    owner = await make_user(pg_session)
    await pg_session.execute(
        insert(SocialProfileLike).values(liker_id=liker.id, profile_user_id=owner.id)
    )

    async with rejects(pg_session, UNIQUE):
        await pg_session.execute(
            insert(SocialProfileLike).values(
                liker_id=liker.id, profile_user_id=owner.id
            )
        )


@pytest.mark.asyncio
async def test_only_one_follow_request_can_be_pending_between_two_people(pg_session):
    """Requests are deleted on accept or decline, so a duplicate row is a
    second ask the target has to dismiss twice — and, since accepting one
    creates the follow edge, the leftover would sit in their queue asking
    them to approve somebody who already follows them."""
    requester = await make_user(pg_session)
    target = await make_user(pg_session)
    await pg_session.execute(
        insert(SocialFollowRequest).values(
            requester_id=requester.id, target_id=target.id
        )
    )

    async with rejects(pg_session, UNIQUE, constraint="uq_social_follow_request"):
        await pg_session.execute(
            insert(SocialFollowRequest).values(
                requester_id=requester.id, target_id=target.id
            )
        )


@pytest.mark.asyncio
async def test_two_images_cannot_occupy_the_same_slide_of_a_post(pg_session):
    """``position`` is the carousel's running order. A post with two slide 0s
    has no defined first image, so the feed thumbnail — and the order the
    author arranged — become whatever the planner returns that day."""
    author = await make_user(pg_session)
    post_id = await _post(pg_session, author)
    await pg_session.execute(
        insert(SocialPostMedia).values(
            post_id=post_id, position=0, storage_key="a.jpg", content_type="image/jpeg"
        )
    )

    async with rejects(pg_session, UNIQUE, constraint="uq_social_post_media_position"):
        await pg_session.execute(
            insert(SocialPostMedia).values(
                post_id=post_id,
                position=0,
                storage_key="b.jpg",
                content_type="image/jpeg",
            )
        )

    # The constraint is on the PAIR: slide 1 of the same post is still fine,
    # which is the half of the rule an over-broad index would have broken.
    await pg_session.execute(
        insert(SocialPostMedia).values(
            post_id=post_id, position=1, storage_key="b.jpg", content_type="image/jpeg"
        )
    )


@pytest.mark.asyncio
async def test_a_collector_can_only_review_a_store_once(pg_session):
    """A store's rating is the average of these rows. One person posting ten
    five-star reviews would move that average, so "edit by re-posting" is
    implemented as this constraint rather than as a rule in the service."""
    reviewer = await make_user(pg_session)
    await pg_session.execute(
        insert(StoreReview).values(
            store_id="osm:node:42", user_id=reviewer.id, rating=5
        )
    )

    async with rejects(pg_session, UNIQUE, constraint="uq_store_review_author"):
        await pg_session.execute(
            insert(StoreReview).values(
                store_id="osm:node:42", user_id=reviewer.id, rating=1
            )
        )


# ── FOREIGN KEY ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_collection_cannot_point_at_a_user_that_does_not_exist(pg_session):
    """This is the constraint SQLite has never enforced for this project:
    foreign keys are off by default there, so an id typo or a stale cached
    user id has always been silently writable in every existing test."""
    async with rejects(pg_session, FOREIGN_KEY):
        await pg_session.execute(
            insert(Collection).values(user_id=uuid.uuid4(), name="Ghost binder")
        )


@pytest.mark.asyncio
async def test_a_post_cannot_showcase_a_card_that_does_not_exist(pg_session):
    """A nullable FK is still an FK. ``card_id`` may be absent, but when it is
    present it has to name a real catalog row — otherwise the post renders a
    deep-link to a card page that 404s."""
    author = await make_user(pg_session)

    async with rejects(pg_session, FOREIGN_KEY):
        await pg_session.execute(
            insert(SocialPost).values(
                author_id=author.id, body="look at this", card_id=uuid.uuid4()
            )
        )


@pytest.mark.asyncio
async def test_a_card_someone_still_owns_cannot_be_deleted_from_the_catalog(pg_session):
    """``graded_cards.card_id`` is ON DELETE RESTRICT, alone among the FKs
    here, and deliberately: a catalog cleanup must never be able to delete
    the cards out from under people's holdings. The refusal arrives on the
    DELETE, which is the FK direction nothing else in this file covers."""
    owner = await make_user(pg_session)
    card = await make_card(pg_session)
    await _graded_card(pg_session, owner, card)

    async with rejects(pg_session, FOREIGN_KEY):
        await pg_session.execute(delete(Card).where(Card.id == card.id))

    # The holding is untouched — the DELETE was refused, not partially applied.
    still_there = await pg_session.scalar(
        select(func.count())
        .select_from(GradedCard)
        .where(GradedCard.card_id == card.id)
    )
    assert still_there == 1


# ── ON DELETE CASCADE ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_a_user_takes_their_collections_with_them(pg_session):
    """Account deletion has to be real deletion. The DELETE below is a single
    statement sent to postgres, so what is under test is the database's ON
    DELETE CASCADE — not SQLAlchemy's ORM cascade, which would not fire here
    and which is not what runs when an operator deletes a row by hand."""
    user = await make_user(pg_session)
    await _collection(pg_session, user)
    assert await _count(pg_session, Collection, Collection.user_id == user.id) == 1

    await pg_session.execute(
        delete(User)
        .where(User.id == user.id)
        .execution_options(synchronize_session=False)
    )

    assert await _count(pg_session, Collection, Collection.user_id == user.id) == 0


@pytest.mark.asyncio
async def test_deleting_a_user_reaches_two_hops_down_to_their_collection_items(
    pg_session,
):
    """The chain is users → collections → collection_items, and only the
    first hop is obvious. The item here deliberately links the deleted user's
    collection to a holding owned by SOMEBODY ELSE, so the only path that can
    remove it is the second hop; if postgres cascaded one level and stopped,
    the item row would survive and this test would catch it.

    That surviving row would not be harmless: it points at another
    collector's holding and would resurface if the collection id were ever
    reused."""
    owner = await make_user(pg_session)
    other = await make_user(pg_session)
    card = await make_card(pg_session)
    graded_id = await _graded_card(pg_session, other, card)
    collection_id = await _collection(pg_session, owner)
    await pg_session.execute(
        insert(CollectionItem).values(
            collection_id=collection_id, graded_card_id=graded_id
        )
    )

    await pg_session.execute(
        delete(User)
        .where(User.id == owner.id)
        .execution_options(synchronize_session=False)
    )

    # Hop 1 and hop 2 both landed…
    assert await _count(pg_session, Collection, Collection.id == collection_id) == 0
    assert (
        await _count(
            pg_session, CollectionItem, CollectionItem.collection_id == collection_id
        )
        == 0
    )
    # …and the cascade stopped at the boundary of what the user owned: the
    # other collector's card is still in their vault.
    assert await _count(pg_session, GradedCard, GradedCard.id == graded_id) == 1


@pytest.mark.asyncio
async def test_deleting_a_post_takes_its_likes_and_comments_with_it(pg_session):
    """Engagement rows are meaningless without the thing they engage with,
    and every count query joins through the post. Orphans here would be
    invisible rows that still hold a foreign key to a live user."""
    author = await make_user(pg_session)
    fan = await make_user(pg_session)
    post_id = await _post(pg_session, author)
    await pg_session.execute(
        insert(SocialPostLike).values(user_id=fan.id, post_id=post_id)
    )
    await pg_session.execute(
        insert(SocialPostComment).values(
            post_id=post_id, author_id=fan.id, body="grail"
        )
    )

    await pg_session.execute(
        delete(SocialPost)
        .where(SocialPost.id == post_id)
        .execution_options(synchronize_session=False)
    )

    assert (
        await _count(pg_session, SocialPostLike, SocialPostLike.post_id == post_id) == 0
    )
    assert (
        await _count(
            pg_session, SocialPostComment, SocialPostComment.post_id == post_id
        )
        == 0
    )


# ── ON DELETE SET NULL ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_removing_a_card_from_the_catalog_leaves_the_post_about_it_standing(
    pg_session,
):
    """This is the behavioural difference SET NULL exists for, and it is one
    word away from CASCADE in the model. The catalog is re-keyed by imports
    we do not control; if that cascaded, a routine catalog cleanup would
    silently delete people's posts — the caption, the photos, the comment
    thread. Instead the post survives and simply stops deep-linking."""
    author = await make_user(pg_session)
    card = await make_card(pg_session)
    post_id = await _post(pg_session, author, card_id=card.id)

    await pg_session.execute(
        delete(Card)
        .where(Card.id == card.id)
        .execution_options(synchronize_session=False)
    )

    row = (
        await pg_session.execute(
            select(SocialPost.id, SocialPost.card_id, SocialPost.body).where(
                SocialPost.id == post_id
            )
        )
    ).one_or_none()
    assert row is not None, "the post was deleted — SET NULL behaved like CASCADE"
    assert row.card_id is None
    assert row.body == "mint copy"


@pytest.mark.asyncio
async def test_deleting_a_reporter_keeps_the_moderation_case_they_filed(pg_session):
    """A moderation case is the audit record of a decision. If deleting an
    account erased the reports it filed, the record of *why* something was
    removed would disappear with the reporter — including when the account
    being deleted is the one that was reported. The row stays; only the
    attribution is dropped."""
    reporter = await make_user(pg_session)
    author = await make_user(pg_session)
    post_id = await _post(pg_session, author)
    case_id = uuid.uuid4()
    await pg_session.execute(
        insert(SocialModerationCase).values(
            id=case_id,
            target_type="post",
            target_id=post_id,
            author_id=author.id,
            source="report",
            reason="spam",
            excerpt="mint copy",
            reporter_id=reporter.id,
        )
    )

    await pg_session.execute(
        delete(User)
        .where(User.id == reporter.id)
        .execution_options(synchronize_session=False)
    )

    row = (
        await pg_session.execute(
            select(
                SocialModerationCase.reporter_id,
                SocialModerationCase.excerpt,
                SocialModerationCase.status,
            ).where(SocialModerationCase.id == case_id)
        )
    ).one_or_none()
    assert row is not None, "the case vanished with its reporter"
    assert row.reporter_id is None
    # The evidence copy is why the case is still reviewable at all.
    assert row.excerpt == "mint copy"
    assert row.status == "open"


# ── CHECK ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_store_review_must_be_between_one_and_five_stars(pg_session):
    """The rating goes straight into an AVG on the store page. A 0 or a 500
    written by a broken client would move that average permanently, and no
    read path re-validates it — this constraint is the only guard."""
    reviewer = await make_user(pg_session)

    for bad_rating in (0, 6, -1, 500):
        async with rejects(
            pg_session, CHECK, constraint="ck_store_reviews_ck_store_review_rating"
        ):
            await pg_session.execute(
                insert(StoreReview).values(
                    store_id="osm:node:7", user_id=reviewer.id, rating=bad_rating
                )
            )

    # Both ends of the range are inclusive.
    for good_rating, store in ((1, "osm:node:1"), (5, "osm:node:5")):
        await pg_session.execute(
            insert(StoreReview).values(
                store_id=store, user_id=reviewer.id, rating=good_rating
            )
        )


@pytest.mark.asyncio
async def test_nobody_can_follow_themselves(pg_session):
    """A self-edge would put you in your own feed and add one to your own
    follower count — a number every profile displays. Cheap to enforce here,
    and impossible to enforce reliably in a service that has several code
    paths creating follows (direct follow, accepted request, seeding)."""
    user = await make_user(pg_session)

    async with rejects(
        pg_session, CHECK, constraint="ck_social_follows_ck_social_follow_not_self"
    ):
        await pg_session.execute(
            insert(SocialFollow).values(follower_id=user.id, followee_id=user.id)
        )


@pytest.mark.asyncio
async def test_nobody_can_like_their_own_profile(pg_session):
    """Same reasoning as self-follow: the like count is a public number, so
    the one person with an incentive to inflate it is barred at the storage
    layer rather than in whichever endpoint happens to be current."""
    user = await make_user(pg_session)

    async with rejects(
        pg_session, CHECK, constraint="ck_social_profile_likes_ck_social_like_not_self"
    ):
        await pg_session.execute(
            insert(SocialProfileLike).values(liker_id=user.id, profile_user_id=user.id)
        )


@pytest.mark.asyncio
async def test_nobody_can_request_to_follow_themselves(pg_session):
    """The self-check is repeated on requests because the request table is a
    second door into the follow graph: accepting a request creates the edge,
    so a self-request that got stored would become a self-follow later."""
    user = await make_user(pg_session)

    async with rejects(
        pg_session,
        CHECK,
        constraint="ck_social_follow_requests_ck_social_request_not_self",
    ):
        await pg_session.execute(
            insert(SocialFollowRequest).values(requester_id=user.id, target_id=user.id)
        )


# ── CLOSED SETS ───────────────────────────────────────────────────────────
#
# Four columns whose models describe a fixed vocabulary and whose types —
# `String(16)`, `Numeric(4, 1)` — describe nothing of the kind. Until
# migration 0056 the three tests below were the inverse of tests: they passed
# by writing a bad row SUCCESSFULLY, because that was the schema as shipped
# and a documented gap beats an invisible one. `store_reviews.rating` was the
# standing proof that this schema was willing to constrain a value, so the
# columns that went unconstrained were a choice somebody could revisit.
#
# What makes these worth enforcing in the database rather than in the service
# is that a bad value here never raises anywhere. It is accepted, stored, and
# then quietly skipped by every query that filters on the column — so the
# symptom is a case that vanishes from the queue, or a portfolio total that
# is wrong, and neither points back at the write that caused it.


@pytest.mark.asyncio
async def test_a_moderation_case_must_use_a_status_target_and_source_the_app_knows(
    pg_session,
):
    """Each of the three vocabularies is closed by postgres, not by good spelling.

    The queue filters on all three, so an unrecognised value does not fail —
    it files a case no query in the app will ever list again. For a
    ``removed`` case that means a takedown with no reviewable record; for an
    open one it means a report the community filed and nobody will ever see.

    ``target_type`` is the one worth reading twice. The legal set is SEVEN
    surfaces, not the three the model's comment used to name: users may only
    report posts, comments and profiles (``safety.TARGET_TYPES``), but the
    classifier files auto-cases against reviews, collections, stories and
    story comments too. A CHECK that had trusted the stale comment would have
    started rejecting every story flag.
    """
    reporter = await make_user(pg_session)
    author = await make_user(pg_session)
    post_id = await _post(pg_session, author)

    async with rejects(
        pg_session,
        CHECK,
        constraint="ck_social_moderation_cases_ck_moderation_case_status",
    ):
        await pg_session.execute(
            insert(SocialModerationCase).values(
                target_type="post",
                target_id=post_id,
                source="report",
                reporter_id=reporter.id,
                status="banana",
            )
        )

    async with rejects(
        pg_session,
        CHECK,
        constraint="ck_social_moderation_cases_ck_moderation_case_target_type",
    ):
        await pg_session.execute(
            insert(SocialModerationCase).values(
                target_type="nonsense",
                target_id=post_id,
                source="report",
                reporter_id=reporter.id,
            )
        )

    async with rejects(
        pg_session,
        CHECK,
        constraint="ck_social_moderation_cases_ck_moderation_case_source",
    ):
        await pg_session.execute(
            insert(SocialModerationCase).values(
                target_type="post",
                target_id=post_id,
                source="not-a-source",
                reporter_id=reporter.id,
            )
        )

    # And the legal vocabulary still writes — including the four surfaces a
    # user cannot report but the classifier can flag, which is the half an
    # over-narrow constraint would have broken.
    for target_type in ("post", "comment", "profile", "review", "collection", "story"):
        await pg_session.execute(
            insert(SocialModerationCase).values(
                target_type=target_type,
                target_id=uuid.uuid4(),
                source="auto",
                status="removed",
            )
        )


@pytest.mark.asyncio
async def test_a_holding_must_carry_a_grade_between_zero_and_ten(pg_session):
    """Grades are 0-10 and postgres now says so; ``Numeric(4, 1)`` allowed 999.9.

    Not a cosmetic rule. The grade drives the price lookup
    (``_GRADE_MULT_HISTORY``, defined for 1 through 10 in half steps) and
    therefore the portfolio total, so one 99.9 from a broken import silently
    inflates a number the user is told their collection is worth — and no
    read path re-validates it.

    The bounds come from the code, not from PSA's rulebook: 10 is the top of
    every house we recognise, and 0 is kept because it is a real value here —
    ``graded_card_service`` writes it as the sentinel for a RAW holding,
    where ``condition`` carries the meaning instead. A ``grade >= 1``
    constraint would have refused every ungraded card in the product.
    """
    owner = await make_user(pg_session)
    card = await make_card(pg_session)

    for bad_grade in (Decimal("99.9"), Decimal("-4.0"), Decimal("10.1")):
        async with rejects(
            pg_session, CHECK, constraint="ck_graded_cards_ck_graded_card_grade"
        ):
            await pg_session.execute(
                insert(GradedCard).values(
                    user_id=owner.id, card_id=card.id, grade=bad_grade
                )
            )

    # Both ends inclusive, and the half steps in between. 0 is the raw
    # sentinel, 10 is gem mint.
    for good_grade in (Decimal("0"), Decimal("9.5"), Decimal("10")):
        graded_id = uuid.uuid4()
        await pg_session.execute(
            insert(GradedCard).values(
                id=graded_id, user_id=owner.id, card_id=card.id, grade=good_grade
            )
        )
        stored = await pg_session.scalar(
            select(GradedCard.grade).where(GradedCard.id == graded_id)
        )
        assert stored == good_grade


@pytest.mark.asyncio
async def test_an_account_cannot_be_put_on_a_plan_nothing_grants_entitlements_for(
    pg_session,
):
    """``plan`` is 'free' or 'pro', and the failure mode of a third value is silence.

    ``entitlement_service`` treats anything that is not "pro" as free, so a
    typo written by a billing backfill or a hand-run psql fix-up does not
    error anywhere — it downgrades a paying customer, and no read path would
    ever surface the bad string. The API's pydantic ``Literal`` already
    refuses it at the edge; this is the same rule for the writers that never
    go through the API.
    """
    user = await make_user(pg_session)

    async with rejects(pg_session, CHECK, constraint="ck_users_ck_user_plan"):
        await pg_session.execute(
            update(User).where(User.id == user.id).values(plan="enterprise")
        )

    # Capitalisation counts: "Pro" is exactly the typo that would have read
    # as free forever.
    async with rejects(pg_session, CHECK, constraint="ck_users_ck_user_plan"):
        await pg_session.execute(
            insert(User).values(email="shouty@example.com", plan="Pro")
        )

    for good_plan in ("free", "pro"):
        await pg_session.execute(
            update(User).where(User.id == user.id).values(plan=good_plan)
        )


# ── SERVER DEFAULTS ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_account_can_be_inserted_without_the_columns_only_the_orm_fills(
    pg_session,
):
    """A raw INSERT of a user works — every NOT NULL flag has a server default.

    Six columns on ``users`` (``is_admin``, ``failed_login_count``,
    ``token_version``, ``mfa_enabled``, ``plan``, ``pro_trialing``) are NOT
    NULL and carried only a PYTHON-side default, which exists solely inside
    SQLAlchemy. Every writer that is not the ORM — a psql backfill, a
    data-repair script, a ``COPY`` restore, the seed SQL in this file's own
    performance suite — therefore hit a bare 23502 on a column with an
    obvious right answer.

    The statement below is deliberately raw text rather than
    ``insert(User)``: going through the ORM would apply the Python defaults
    and prove nothing at all.
    """
    await pg_session.execute(
        text(
            "INSERT INTO users (id, email) "
            "VALUES (gen_random_uuid(), 'raw-insert@example.test')"
        )
    )

    row = (
        await pg_session.execute(
            select(
                User.is_admin,
                User.failed_login_count,
                User.token_version,
                User.mfa_enabled,
                User.plan,
                User.pro_trialing,
            ).where(User.email == "raw-insert@example.test")
        )
    ).one()

    # The server defaults must agree with the Python ones, or a row's history
    # would depend on which writer created it.
    assert row.is_admin is False
    assert row.failed_login_count == 0
    assert row.token_version == 0
    assert row.mfa_enabled is False
    assert row.plan == "free"
    assert row.pro_trialing is False


# ── IMMEDIATE, NOT DEFERRED ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_broken_foreign_key_is_refused_by_the_statement_not_by_the_commit(
    pg_session,
):
    """WHEN a constraint fires decides how the app can be written.

    Because these FKs are immediate, the failing INSERT raises where it was
    written — the service sees the error next to the row that caused it and
    can map it to a 404/409. Were they deferred, every violation would
    surface at ``session.commit()``, far from the code that made it, as a
    single error naming a constraint but not a request.

    The ``SET CONSTRAINTS ALL DEFERRED`` below makes the point sharply: it is
    the only lever postgres offers for postponing checks, and on a NOT
    DEFERRABLE constraint it does nothing at all. No commit is called
    anywhere in this test.
    """
    await pg_session.execute(text("SET CONSTRAINTS ALL DEFERRED"))

    async with rejects(pg_session, FOREIGN_KEY):
        await pg_session.execute(
            insert(Collection).values(user_id=uuid.uuid4(), name="Ghost binder")
        )


@pytest.mark.asyncio
async def test_no_foreign_key_in_the_schema_is_deferrable(pg_session):
    """The guarantee above, stated once for the whole schema instead of one
    table at a time. A deferrable FK slipping into a future migration would
    move that table's errors from the statement to the commit — a change in
    where every failure appears, invisible in a diff unless something looks."""
    rows = (
        await pg_session.execute(
            text(
                "SELECT conrelid::regclass::text AS tbl, conname "
                "FROM pg_constraint "
                "WHERE contype = 'f' AND condeferrable "
                "AND connamespace = 'public'::regnamespace"
            )
        )
    ).all()

    assert rows == [], f"deferrable foreign keys: {[(r.tbl, r.conname) for r in rows]}"
