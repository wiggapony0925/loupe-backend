"""ORM models for the social layer (profiles, follow graph, requests)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol


class SocialProfile(Base):
    """A user's public-facing community identity (1:1 with ``users``).

    Kept separate from ``User`` so the account row stays auth/billing-only
    and the social surface can evolve on its own. A user has no social
    presence until they claim a username here.
    """

    __tablename__ = "social_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Stored lowercase; uniqueness is therefore case-insensitive by
    # construction (no dialect-specific citext needed).
    username: Mapped[str] = mapped_column(
        String(30), unique=True, index=True, nullable=False
    )
    bio: Mapped[str | None] = mapped_column(String(280), nullable=True)
    # Free-text "City, Region" the user chooses to share — never derived
    # from GPS/IP, so showing it publicly is their explicit call.
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Instagram semantics: private profiles require an approved follow
    # request before their collection (and follower lists) are visible.
    is_private: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)
    # Object key of the uploaded profile picture in blob storage; NULL until
    # one is uploaded. `avatar_version` bumps on every upload so the public
    # avatar URL is cache-busted (`?v=N`) without signed URLs.
    avatar_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_content_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SocialFollow(Base):
    """Follower edge: ``follower_id`` follows ``followee_id`` (accepted)."""

    __tablename__ = "social_follows"
    __table_args__ = (
        CheckConstraint("follower_id != followee_id", name="ck_social_follow_not_self"),
        # PK covers (follower → followee) lookups; this serves the reverse
        # direction (who follows X) for follower lists + counts.
        Index("ix_social_follows_followee", "followee_id"),
    )

    follower_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    followee_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SocialFollowRequest(Base):
    """A pending ask to follow a private account.

    Rows exist only while pending — accepting converts the row into a
    :class:`SocialFollow` and deletes it; declining just deletes it.
    """

    __tablename__ = "social_follow_requests"
    __table_args__ = (
        UniqueConstraint("requester_id", "target_id", name="uq_social_follow_request"),
        CheckConstraint("requester_id != target_id", name="ck_social_request_not_self"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    requester_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SocialProfileLike(Base):
    """One collector's appreciation of another's collection.

    A like is an edge, not a counter, so it can be un-done, attributed, and
    counted honestly. Storing a running integer on the profile instead would
    have made "unlike" unimplementable without a second source of truth about
    who had already liked.
    """

    __tablename__ = "social_profile_likes"
    __table_args__ = (
        CheckConstraint("liker_id != profile_user_id", name="ck_social_like_not_self"),
        # PK is (liker → profile); this serves the count/`has_liked` direction.
        Index("ix_social_profile_likes_profile", "profile_user_id"),
    )

    liker_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    profile_user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SocialProfileVisit(Base):
    """A distinct collector who has looked at this profile.

    Deliberately *unique viewers*, not hits: one row per (viewer, profile),
    upserted on conflict. Counting raw page loads would let a single curious
    user — or the owner's own refreshes — inflate the number into something
    meaningless, and it would grow this table without bound. Unique viewers
    stays bounded by the follow graph's realistic size and is the figure a
    collector actually wants ("how many people have seen my vault").

    ``last_seen_at`` is refreshed on repeat visits so the row can support
    "recent visitors" later without a schema change.
    """

    __tablename__ = "social_profile_visits"
    __table_args__ = (
        CheckConstraint(
            "viewer_id != profile_user_id", name="ck_social_visit_not_self"
        ),
        Index("ix_social_profile_visits_profile", "profile_user_id"),
    )

    viewer_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    profile_user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── The feed (posts, comments, hashtags) ──
#
# Deliberately WITHOUT denormalised like/comment counters. A counter column
# is the classic source of a feed that lies: every path that inserts or
# deletes an edge has to remember to move it, and one missed rollback leaves
# a number nobody can explain. Feeds here read a page of ~20 posts and then
# aggregate the counts for exactly those ids in one grouped query, which is
# both exact and cheap. If the post volume ever outgrows that, counters can
# be added as a cache over this truth rather than in place of it.


class SocialPost(Base):
    """One post in the community feed.

    A post is a caption plus zero or more images, optionally *about* a card
    in the catalog. The card link is what makes this a collector's feed
    rather than a generic photo app: a showcase post can deep-link straight
    to the card page and the price the poster is talking about.
    """

    __tablename__ = "social_posts"
    __table_args__ = (
        # "This author's posts, newest first" — the profile grid and the
        # Your Posts tab.
        Index("ix_social_posts_author_created", "author_id", "created_at"),
        # The global window every discovery feed scans before ranking.
        Index("ix_social_posts_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The caption. Hashtags and @mentions are extracted out of this text
    #: into their own tables on write, but the raw text is kept verbatim so
    #: clients render exactly what the author typed.
    body: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Optional catalog card this post showcases. SET NULL rather than
    #: CASCADE — a catalog re-key must never silently delete someone's post.
    card_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(),
        ForeignKey("cards.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Soft delete, matching holdings: an author removing a post shouldn't
    #: vaporise the comment thread other people wrote under it.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: THE FEED'S SORT KEY, which is why it carries a Python-side default on
    #: top of the server one. Two things depend on that:
    #:
    #: * Precision. ``CURRENT_TIMESTAMP`` on SQLite has no fractional part,
    #:   so posts made in the same second would have no defined order and
    #:   keyset pagination would have nothing to seek by.
    #: * Symmetry. A cursor is a value read back out of this column and bound
    #:   again as a parameter. When the row was written by the server default
    #:   and the cursor by the driver, the two render differently
    #:   ("…01:44:58" vs "…01:44:58.000000") and a lexicographic comparison
    #:   silently matches every row — a feed that pages forever, repeating
    #:   itself. Writing both through the same path removes the mismatch.
    #:
    #: ``server_default`` stays for rows inserted outside the ORM.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SocialPostMedia(Base):
    """One image attached to a post.

    Separate rows rather than a JSON column so ``position`` can be a real
    ordering the DB enforces (a carousel with two slide 1s is a bug) and so
    a single image can be served, cached and deleted on its own id.
    """

    __tablename__ = "social_post_media"
    __table_args__ = (
        UniqueConstraint("post_id", "position", name="uq_social_post_media_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    post_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("social_posts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    #: 0-based slide order within the post.
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Object key in the blob store (see app/social/post_media.py).
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Intrinsic pixel size, recorded at upload. Clients need the aspect
    #: ratio BEFORE the bytes arrive — without it every feed image pops the
    #: layout when it loads, which is the single most visible jank in a
    #: scrolling feed.
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SocialPostLike(Base):
    """``user_id`` likes ``post_id``. An edge, so it can be undone."""

    __tablename__ = "social_post_likes"
    __table_args__ = (Index("ix_social_post_likes_post", "post_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    post_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("social_posts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SocialPostComment(Base):
    """A comment, or a reply to one.

    Threading is deliberately ONE level deep, like Instagram: ``parent_id``
    always points at a top-level comment. Replying to a reply attaches to
    that reply's parent (the service flattens it) so a thread can never
    become an unreadable staircase, and so the reply count under a top-level
    comment is a single query rather than a recursive walk.
    """

    __tablename__ = "social_post_comments"
    __table_args__ = (
        # The thread query: this post's top-level comments, oldest first.
        Index("ix_social_post_comments_post_created", "post_id", "created_at"),
        Index("ix_social_post_comments_parent", "parent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    post_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("social_posts.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(),
        ForeignKey("social_post_comments.id", ondelete="CASCADE"),
        nullable=True,
    )
    body: Mapped[str] = mapped_column(Text(), nullable=False)
    #: Python-side default for the same reason as SocialPost.created_at: this
    #: orders the thread, and second-granularity timestamps would leave two
    #: comments posted in the same second in arbitrary order.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )


class SocialCommentLike(Base):
    """``user_id`` likes ``comment_id`` — the heart beside a comment."""

    __tablename__ = "social_comment_likes"
    __table_args__ = (Index("ix_social_comment_likes_comment", "comment_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    comment_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("social_post_comments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SocialPostHashtag(Base):
    """One ``#tag`` extracted from a post's caption.

    Stored as the bare lowercase word (no ``#``) on the join row itself
    rather than behind a `hashtags` entity table. A tag has no properties of
    its own here — trending is ``GROUP BY tag`` over a time window and the
    tag feed is an equality lookup — so a second table would add a join to
    every read and buy nothing.
    """

    __tablename__ = "social_post_hashtags"
    __table_args__ = (
        # Trending (group by tag over recent posts) and the tag feed.
        Index("ix_social_post_hashtags_tag", "tag"),
    )

    post_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("social_posts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: Lowercase, no leading '#'.
    tag: Mapped[str] = mapped_column(String(64), primary_key=True)


class SocialPostMention(Base):
    """An ``@handle`` in a caption, resolved to the account it names.

    Resolved at write time, not render time: the notification has to fire
    once when the post is created, and a client shouldn't need a lookup per
    handle to know which words in a caption are real people.
    """

    __tablename__ = "social_post_mentions"
    __table_args__ = (Index("ix_social_post_mentions_user", "user_id"),)

    post_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("social_posts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )


class SocialModerationCase(Base):
    """One thing a human may need to look at.

    Auto-flags from the classifier and reports from users live in the SAME
    table on purpose. A moderator's question is "what needs my attention,
    worst first" — not "which system noticed". Two tables would mean two
    queues, two UIs, and the near-certainty that one of them goes unread.

    ``target_id`` carries no foreign key: a case can point at a post, a
    comment or a profile, and those are three tables. The trade is that a
    hard-deleted target leaves a dangling case — acceptable, because the
    case is also the audit record of *why* it was deleted.
    """

    __tablename__ = "social_moderation_cases"
    __table_args__ = (
        # The queue: open cases, newest first.
        Index("ix_social_moderation_status_created", "status", "created_at"),
        Index("ix_social_moderation_target", "target_type", "target_id"),
        # One report per person per thing — reporting twice is not a vote,
        # and letting it be one turns the queue into a brigading tool.
        UniqueConstraint(
            "target_type",
            "target_id",
            "reporter_id",
            name="uq_social_moderation_reporter",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    #: "post" | "comment" | "profile".
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UuidCol(), nullable=False)
    #: Who wrote the thing. SET NULL so deleting an account doesn't erase the
    #: record of a decision made about it.
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: "auto" (the classifier) | "report" (a user).
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Classifier categories, or the reporter's chosen reason.
    reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: The reporter's note, or the classifier's explanation.
    detail: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Worst classifier score, 0-1. Orders the queue by severity.
    score: Mapped[float | None] = mapped_column(Float(), nullable=True)
    #: A COPY of the offending text, kept because the target may be deleted
    #: before anyone reviews it and "removed something, don't know what" is
    #: not a reviewable record.
    excerpt: Mapped[str | None] = mapped_column(Text(), nullable=True)
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: "open" | "dismissed" | "removed".
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )


class StoreReview(Base):
    """A collector's review of a physical card shop.

    Stores are UPSTREAM entities (``osm:node:123``), not rows we own, so
    the key is the opaque store id the locator emits — no FK, and reviews
    survive a store's catalog data changing. One review per user per store
    (edit by re-posting); ratings are 1-5 whole stars like every venue app.
    """

    __tablename__ = "store_reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    #: Locator id, e.g. "osm:node:1234567" — opaque to us.
    store_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("store_id", "user_id", name="uq_store_review_author"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_store_review_rating"),
        Index("ix_store_reviews_store_created", "store_id", "created_at"),
    )


class SavedStore(Base):
    """A shop a collector hearted — their saved places.

    Keyed on the UPSTREAM store id like StoreReview: stores aren't rows we
    own, so a save survives the catalog data changing underneath it.
    """

    __tablename__ = "saved_stores"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    store_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "store_id", name="uq_saved_store_user"),
        Index("ix_saved_stores_user_created", "user_id", "created_at"),
    )


__all__ = [
    "SavedStore",
    "SocialCommentLike",
    "SocialFollow",
    "SocialFollowRequest",
    "SocialModerationCase",
    "SocialPost",
    "SocialPostComment",
    "SocialPostHashtag",
    "SocialPostLike",
    "SocialPostMedia",
    "SocialPostMention",
    "SocialProfile",
    "SocialProfileLike",
    "SocialProfileVisit",
    "StoreReview",
]
