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
    #: "People your friends follow", ranked by how many of the viewer's
    #: followees also follow them. The strongest signal a social graph has,
    #: and the antidote to every new user seeing the same six accounts.
    #: Empty until the viewer follows somebody.
    followed_by_friends: list[SocialUserCard] = []
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
    "MAX_COMMENT_BODY",
    "MAX_POST_BODY",
    "USERNAME_PATTERN",
    "CommentCreate",
    "CommentRead",
    "CommentThreadRead",
    "DiscoverRead",
    "ExploreCard",
    "ExploreRead",
    "FeedRead",
    "FeedTab",
    "FollowRequestRead",
    "FollowStateRead",
    "FriendOwnerRead",
    "HashtagRead",
    "ModerationCaseRead",
    "ModerationQueueRead",
    "PostAuthor",
    "PostCardRef",
    "PostLikeRead",
    "PostMediaRead",
    "PostRead",
    "RelationshipState",
    "ReportCreate",
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
    "SocialSearchRead",
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


# ── The feed ──

#: Which feed a client is asking for. The backend owns what each one MEANS
#: (who is in it, how it's ordered) so web and native can never drift.
FeedTab = Literal["following", "foryou", "mine"]

#: Caption/comment caps. Generous enough for a real write-up, bounded so a
#: single row can't be used as blob storage.
MAX_POST_BODY = 2200
MAX_COMMENT_BODY = 1000


class PostAuthor(BaseModel):
    """Who wrote a post or comment.

    Deliberately leaner than :class:`SocialUserCard`: a feed page shows 20
    authors and a card would drag a collection-peek query along with each
    one. Everything a byline draws is here and nothing else.
    """

    user_id: uuid.UUID
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    #: Gold PRO chip — raw plan, same rule as SocialUserCard.is_pro.
    is_pro: bool = False
    #: Loupe staff — drives the verified tick.
    is_admin: bool = False
    relationship: RelationshipState = "none"


class PostMediaRead(BaseModel):
    """One slide in a post's carousel — a photo or a video."""

    id: uuid.UUID
    url: str
    position: int = 0
    #: Intrinsic pixel size when known. Clients reserve the aspect ratio
    #: BEFORE the bytes arrive; without it every image pops the layout.
    width: int | None = None
    height: int | None = None
    #: ``"image"`` or ``"video"``. Sent as a KIND rather than leaving the
    #: clients to sniff the MIME type: three of them would each write their
    #: own `startsWith("video/")`, and the first codec added off this list
    #: would break whichever one was forgotten.
    kind: str = "image"
    content_type: str = "image/jpeg"


class PostCardRef(BaseModel):
    """The catalog card a post showcases — what makes this a collector's
    feed rather than a photo app. Enough to draw a tappable chip.

    Carries NO price on purpose. A feed row is a snapshot of a moment and
    would go stale in place, while the card page it links to holds the
    authoritative, grade-aware number; a second figure drifting beside it
    would only ever be a source of "which one is right?".
    """

    card_id: uuid.UUID
    name: str | None = None
    image_url: str | None = None
    set_name: str | None = None
    number: str | None = None
    tcg: str | None = None


class PostRead(BaseModel):
    """One post as the viewer sees it."""

    id: uuid.UUID
    author: PostAuthor
    body: str | None = None
    media: list[PostMediaRead] = []
    card: PostCardRef | None = None
    created_at: datetime
    #: When the caption was last rewritten, or None. Clients render
    #: "· edited" beside the timestamp — comments underneath may be
    #: answering words that are no longer there.
    edited_at: datetime | None = None
    like_count: int = 0
    comment_count: int = 0
    #: Drives the filled heart — computed for the requesting viewer.
    viewer_has_liked: bool = False
    #: Tags extracted from the caption, lowercase and without '#'. Clients
    #: highlight them by matching the caption text; this list is what a tag
    #: chip row and the "posts in #x" link are built from.
    hashtags: list[str] = []
    #: Handles named in the caption that resolve to real accounts, so a
    #: client can link only the @words that actually go somewhere.
    mentions: list[str] = []
    #: Whether the viewer may remove this post (author or staff).
    can_delete: bool = False
    #: Author only — staff can remove a post but never rewrite one under
    #: someone else's byline. Sent rather than derived so the clients don't
    #: each re-implement the rule and disagree about the staff case.
    can_edit: bool = False


# ── Stories ──


class StoryRead(BaseModel):
    """One story card."""

    id: uuid.UUID
    author: PostAuthor
    url: str
    kind: str = "image"
    content_type: str = "image/jpeg"
    width: int | None = None
    height: int | None = None
    #: Video length. NULL for a still, which the client shows for its own
    #: fixed dwell instead.
    duration_ms: int | None = None
    caption: str | None = None
    created_at: datetime
    expires_at: datetime
    #: Whether the VIEWER has already opened this one — drives the ring.
    seen: bool = False
    #: Author only; 0 for everyone else. Nobody gets to see how many people
    #: watched someone else's story.
    view_count: int = 0
    comment_count: int = 0
    can_delete: bool = False


class StoryTrayEntry(BaseModel):
    """One avatar in the tray above the feed.

    Composed server-side — order, unseen state and the preview frame all
    come from here, so the row can't be assembled differently on two
    clients and disagree about whose ring is lit.
    """

    author: PostAuthor
    story_count: int
    #: Lights the ring. False once every one of their live stories is seen.
    has_unseen: bool
    #: Newest story's timestamp, for "3h" under the avatar.
    latest_at: datetime
    #: First frame to show behind the avatar while the viewer loads.
    preview_url: str | None = None


class StoryTrayRead(BaseModel):
    """The whole tray. `mine` is separated because it renders as the
    "Your story" plus-button slot rather than as another avatar."""

    mine: StoryTrayEntry | None = None
    entries: list[StoryTrayEntry] = []


class StoryCommentRead(BaseModel):
    """One comment under a story.

    A comment, not a DM — see :class:`app.social.models.SocialStoryComment`.
    """

    id: uuid.UUID
    story_id: uuid.UUID
    author: PostAuthor
    body: str
    created_at: datetime
    can_delete: bool = False


class StoryCommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=500)


class StoryViewerRead(BaseModel):
    """A person who opened your story. Author-only."""

    viewer: PostAuthor
    viewed_at: datetime


class PostEdit(BaseModel):
    """A caption rewrite. Images are not editable — see `edit_post`."""

    body: str | None = None


class FeedRead(BaseModel):
    """A page of posts. ``next_cursor`` is opaque — pass it back verbatim.

    Cursors rather than offsets: a feed gains rows at the top while it's
    being read, and offset paging responds to that by showing page 2's first
    post twice. The token encodes the position, so a page is stable no
    matter what arrives above it.
    """

    items: list[PostRead] = []
    next_cursor: str | None = None


class CommentRead(BaseModel):
    """One comment, or one reply (``parent_id`` set)."""

    id: uuid.UUID
    post_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    author: PostAuthor
    body: str
    created_at: datetime
    like_count: int = 0
    viewer_has_liked: bool = False
    #: Replies hanging off this top-level comment. Always 0 on a reply —
    #: threading is one level deep.
    reply_count: int = 0
    #: The first few replies, inlined. A thread where every comment needs a
    #: round trip to show its two replies feels broken; the rest come from
    #: the replies endpoint behind "View N replies".
    replies: list[CommentRead] = []
    can_delete: bool = False


class CommentThreadRead(BaseModel):
    """``GET /v1/social/posts/{id}/comments`` — top-level comments, oldest
    first (a conversation reads down, unlike a feed)."""

    items: list[CommentRead] = []
    next_cursor: str | None = None
    #: Every comment on the post, replies included — the number under the
    #: post's speech bubble.
    total: int = 0


class CommentCreate(BaseModel):
    """Body for ``POST /v1/social/posts/{id}/comments``."""

    body: str = Field(..., min_length=1, max_length=MAX_COMMENT_BODY)
    #: Set to reply. Replying to a reply attaches to its top-level parent —
    #: the server flattens it, so clients can pass whatever was tapped.
    parent_id: uuid.UUID | None = None


class PostLikeRead(BaseModel):
    """New like state + the fresh total, so the client never guesses."""

    liked: bool
    like_count: int = 0


class HashtagRead(BaseModel):
    """One tag chip."""

    tag: str
    post_count: int = 0


# ── Safety ──


class ReportCreate(BaseModel):
    """Body for ``POST /v1/social/reports``."""

    target_type: Literal["post", "comment", "profile"]
    target_id: uuid.UUID
    #: One of `safety.REPORT_REASONS`. A closed list on purpose — free text
    #: produces "idk it's weird", which can't be counted or acted on.
    reason: str = Field(..., max_length=32)
    note: str | None = Field(None, max_length=1000)


class ModerationCaseRead(BaseModel):
    """One row in the review queue."""

    id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    author_id: uuid.UUID | None = None
    author_username: str | None = None
    #: "auto" (the classifier) | "report" (a user).
    source: str
    reason: str | None = None
    reason_label: str | None = None
    detail: str | None = None
    #: Worst classifier score, 0-1. Orders the queue by severity.
    score: float | None = None
    #: A copy of the text, kept in case the target is deleted before review.
    excerpt: str | None = None
    reporter_username: str | None = None
    status: str
    created_at: datetime
    resolved_at: datetime | None = None


class ModerationQueueRead(BaseModel):
    items: list[ModerationCaseRead] = []
    total: int = 0
    #: Open cases regardless of the filter — the badge on the nav.
    open_count: int = 0


class SocialSearchRead(BaseModel):
    """``GET /v1/social/search/all`` — one query, both kinds of result.

    Composed server-side so the search bar's ranking (which handles beat
    which tags) is one decision in one place rather than two lists the
    clients have to interleave themselves.
    """

    users: list[SocialUserCard] = []
    hashtags: list[HashtagRead] = []
