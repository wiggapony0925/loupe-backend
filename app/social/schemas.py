"""Pydantic schemas for the social API surface."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# How the viewer relates to a profile. `requested` = a pending follow
# request on a private account.
RelationshipState = Literal["self", "following", "requested", "none"]

USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._]{2,29}$"


class SocialProfileUpsert(BaseModel):
    """Body for ``PUT /v1/social/me`` — claims or updates the profile."""

    username: str = Field(
        ...,
        min_length=3,
        max_length=30,
        pattern=USERNAME_PATTERN,
        description="Handle (letters, digits, dot, underscore). Stored lowercase.",
    )
    bio: str | None = Field(None, max_length=280)
    location: str | None = Field(
        None, max_length=120, description="Self-reported 'City, Region' — optional."
    )
    is_private: bool = False


class SocialProfileRead(BaseModel):
    """The caller's own social profile."""

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    username: str
    bio: str | None = None
    location: str | None = None
    is_private: bool = False
    avatar_url: str | None = None
    created_at: datetime


class SocialMeRead(BaseModel):
    """``GET /v1/social/me`` — profile (null until claimed) + inbox badge."""

    profile: SocialProfileRead | None = None
    incoming_request_count: int = 0


class SocialUserCard(BaseModel):
    """One row in search results / follower lists (Collectr-style row)."""

    user_id: uuid.UUID
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    location: str | None = None
    is_private: bool = False
    # Paying/comped Loupe Pro — drives the gold PRO chip (Collectr-style).
    # Raw ``users.plan``, NOT the effective entitlement: with the
    # ``subscriptions_enabled`` kill switch off everyone is *treated* as Pro,
    # and a badge that lights up for every account would mean nothing.
    is_pro: bool = False
    relationship: RelationshipState = "none"


class SocialProfileView(BaseModel):
    """``GET /v1/social/users/{username}`` — a profile as seen by the viewer."""

    user_id: uuid.UUID
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    location: str | None = None
    is_private: bool = False
    is_pro: bool = False
    joined_at: datetime
    follower_count: int = 0
    following_count: int = 0
    card_count: int = 0
    #: Collectors who have appreciated this collection.
    like_count: int = 0
    #: DISTINCT collectors who have opened this profile — not raw page hits,
    #: and never the owner's own visits. See SocialProfileVisit.
    view_count: int = 0
    #: Whether the *requesting* viewer has liked it (drives the filled heart).
    viewer_has_liked: bool = False
    relationship: RelationshipState = "none"
    # Whether the viewer may see the collection / follower lists (public
    # account, own profile, or an accepted follower of a private one).
    can_view_collection: bool = False


class ProfileLikeRead(BaseModel):
    """Result of a like/unlike — the new state plus the fresh total.

    Returning the count means the client never has to guess or refetch the
    profile to keep the heart and the number in agreement.
    """

    model_config = ConfigDict(from_attributes=True)

    liked: bool
    like_count: int = 0


class FollowStateRead(BaseModel):
    """Result of a follow/unfollow action — the new relationship."""

    relationship: RelationshipState


class FollowRequestRead(BaseModel):
    """One pending incoming follow request (the requester's card)."""

    id: uuid.UUID
    requester: SocialUserCard
    created_at: datetime


class SocialCollectionItem(BaseModel):
    """A holding as shown to OTHER collectors — deliberately excludes the
    owner's cost basis (purchase price/date) and private notes."""

    id: uuid.UUID
    # Catalog card id — lets clients deep-link the tile to the card page
    # (web ``/cards/{id}``, native card screen via the WebView detour).
    card_id: uuid.UUID
    card_name: str | None = None
    card_image_url: str | None = None
    card_set_name: str | None = None
    card_number: str | None = None
    card_tcg: str | None = None
    grade: Decimal
    house: str
    condition: str | None = None
    estimated_value_usd: Decimal | None = None
    graded_at: datetime


class SocialCollectionRead(BaseModel):
    """``GET /v1/social/users/{username}/collection`` — privacy-gated vault."""

    total_cards: int = 0
    # Sum of grade-aware holding values (same basis as /v1/grades/summary).
    estimated_value_usd: Decimal | None = None
    items: list[SocialCollectionItem] = []


__all__ = [
    "USERNAME_PATTERN",
    "FollowRequestRead",
    "FollowStateRead",
    "RelationshipState",
    "SocialCollectionItem",
    "SocialCollectionRead",
    "SocialMeRead",
    "SocialProfileRead",
    "SocialProfileUpsert",
    "SocialProfileView",
    "SocialUserCard",
]
