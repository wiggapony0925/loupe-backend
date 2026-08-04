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


__all__ = ["SocialFollow", "SocialFollowRequest", "SocialProfile"]
