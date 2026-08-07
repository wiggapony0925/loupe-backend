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
    #: Loupe staff — drives the ADMIN tag on rows. Effective admin (the DB
    #: flag OR the ADMIN_EMAILS allowlist), so the bootstrap super-admin is
    #: badged too without needing the column set.
    is_admin: bool = False
    relationship: RelationshipState = "none"
    # A PEEK AT WHAT THEY COLLECT. Without these a collector directory is a
    # list of names on an app whose entire subject is the cards — the client
    # had nothing to show but an avatar and a Follow button.
    #: How many cards they own (0 when private — never leak a private size).
    card_count: int = 0
    #: Art for their best few cards, most valuable first. Empty for private
    #: accounts and for collectors with nothing yet.
    preview_image_urls: list[str] = []


class FriendOwnerRead(SocialUserCard):
    """A collector the viewer follows who owns the card in question —
    powers the "N of your friends own this card" strip on card detail."""

    copies: int = 1


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


class DiscoverRead(BaseModel):
    """``GET /v1/social/discover`` — the Community page, composed server-side.

    ``featured`` and ``more`` are guaranteed DISJOINT and ranked by the
    backend (most-followed, then largest collection, then newest), so
    clients render the two shelves verbatim — no slicing, no dedupe.
    """

    featured: list[SocialUserCard] = []
    more: list[SocialUserCard] = []


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
    # The row's price trend, so a card list on a profile carries the same
    # sparkline the owner sees in their own vault. Computed from the card's
    # real price history via the SAME helper the vault endpoint uses; a card
    # with no history gets a flat line rather than invented motion.
    spark_points: list[float] = []
    spark_delta_pct: float | None = None


class SocialPortfolioRead(BaseModel):
    """One of the collector's CURATED collections (binders/decks) — the
    thing users mean by "my collections", distinct from catalog sets."""

    id: uuid.UUID
    name: str
    color: str | None = None
    count: int = 0
    estimated_value_usd: Decimal | None = None
    cover_image_url: str | None = None


class SocialCollectionSet(BaseModel):
    """One set within a shared collection — "they have 5 Evolving Skies"."""

    name: str
    count: int = 0
    estimated_value_usd: Decimal | None = None
    # Art of the set's most valuable card — the rail tile's cover.
    cover_image_url: str | None = None


class SocialSealedItem(BaseModel):
    """One sealed SKU a collector holds (boxes, ETBs, bundles…)."""

    product_id: uuid.UUID
    name: str
    set_name: str | None = None
    product_type: str
    tcg: str
    image_url: str | None = None
    quantity: int = 1
    # TOTAL for the row (unit value x quantity) — matches /v1/grades/summary.
    estimated_value_usd: Decimal | None = None


class SocialCollectionRead(BaseModel):
    """``GET /v1/social/users/{username}/collection`` — privacy-gated vault."""

    total_cards: int = 0
    # Sum of grade-aware holding values (same basis as /v1/grades/summary).
    estimated_value_usd: Decimal | None = None
    # Sealed rollup: UNOPENED holdings only, value = unit x quantity — the
    # exact combinedValueUsd basis the vault summary uses.
    sealed_count: int = 0
    sealed_value_usd: Decimal | None = None
    # Cards + sealed — the headline number a profile should show.
    total_value_usd: Decimal | None = None
    # The collector's curated portfolios (binders), largest value first.
    portfolios: list[SocialPortfolioRead] = []
    # Sealed products shelf, largest value first (server-capped like sets).
    sealed: list[SocialSealedItem] = []
    # How many sets the vault spans in total — ``sets`` is capped to the top
    # few by value, so the client can say "+N more" without math.
    total_sets: int = 0
    # Whole-collection set breakdown (not page-scoped), largest value first.
    sets: list[SocialCollectionSet] = []
    items: list[SocialCollectionItem] = []


class SocialPortfolioItemsRead(BaseModel):
    """``GET /v1/social/users/{username}/collections/{id}`` — one binder,
    drilled into: the cards inside a single curated portfolio."""

    id: uuid.UUID
    name: str
    color: str | None = None
    count: int = 0
    estimated_value_usd: Decimal | None = None
    items: list[SocialCollectionItem] = []


__all__ = [
    "USERNAME_PATTERN",
    "DiscoverRead",
    "ExploreCard",
    "ExploreRead",
    "FollowRequestRead",
    "FollowStateRead",
    "FriendOwnerRead",
    "RelationshipState",
    "SocialCollectionItem",
    "SocialCollectionRead",
    "SocialCollectionSet",
    "SocialMeRead",
    "SocialPortfolioItemsRead",
    "SocialPortfolioRead",
    "SocialProfileRead",
    "SocialProfileUpsert",
    "SocialProfileView",
    "SocialSealedItem",
    "SocialUserCard",
]


class ExploreCard(BaseModel):
    """One tile in the Explore mosaic — a card somebody owns."""

    #: Holding id, unique per tile.
    id: uuid.UUID
    #: Catalog card, so a tap can deep-link the card page.
    card_id: uuid.UUID
    card_name: str | None = None
    image_url: str
    #: Whose it is — the tile's route to a person.
    username: str
    #: Drives the mosaic's occasional double-size tile: the standouts get
    #: the big cell, so the grid has rhythm instead of uniform monotony.
    is_hero: bool = False


class ExploreRead(BaseModel):
    """``GET /v1/social/explore`` — the Community browse grid.

    Cards from PUBLIC collections only, ranked and laid out server-side so
    every client renders the same mosaic verbatim (house rule).
    """

    cards: list[ExploreCard] = []
