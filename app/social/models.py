"""ORM models for the social layer (profiles, follow graph, requests)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
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
    "SocialFollow",
    "SocialFollowRequest",
    "SocialProfile",
    "SocialProfileLike",
    "SocialProfileVisit",
    "StoreReview",
]
