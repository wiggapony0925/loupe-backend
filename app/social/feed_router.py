"""HTTP surface for the community feed (``/v1/social`` continued).

Split from :mod:`app.social.router` — which owns profiles and the follow
graph — because the feed is its own vertical: posts, comments, hashtags and
their images. Same prefix, so to a client it is one API.
"""

from __future__ import annotations

import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.platform.rate_limit import rate_limit
from app.social import post_media, service
from app.social.models import SocialPostMedia
from app.social.schemas import (
    MAX_POST_BODY,
    CommentCreate,
    CommentRead,
    CommentThreadRead,
    FeedRead,
    HashtagRead,
    ModerationCaseRead,
    PostLikeRead,
    PostRead,
    ReportCreate,
    SocialSearchRead,
)
from app.social.services import comments as comments_service
from app.social.services import hashtags as hashtags_service
from app.social.services import posts as posts_service
from app.social.services import safety
from app.social.services.feed_common import DEFAULT_PAGE, MAX_PAGE

router = APIRouter(prefix="/social", tags=["social-feed"])

# Posting is expensive (image bytes + blob writes); reading is not. Comments
# and likes sit in between — fast enough to feel instant, bounded enough
# that a script can't flood someone's thread.
post_limit = rate_limit(limit=20, window_seconds=3600, name="social.post")
comment_limit = rate_limit(limit=60, window_seconds=300, name="social.comment")
reaction_limit = rate_limit(limit=180, window_seconds=60, name="social.reaction")
feed_limit = rate_limit(limit=240, window_seconds=60, name="social.feed")


# ── Feeds ──


@router.get(
    "/feed",
    response_model=FeedRead,
    summary="The community feed (following · for you · mine)",
    dependencies=[Depends(feed_limit)],
)
async def get_feed(
    tab: str = Query(
        "following",
        description=(
            "`following` — people you follow, plus your own posts, newest "
            "first. `foryou` — recent posts ranked by engagement, decayed by "
            "age, excluding your own. `mine` — just your posts."
        ),
    ),
    cursor: str | None = Query(None, description="Opaque; pass back verbatim."),
    limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> FeedRead:
    """What each tab MEANS is decided here, not in the clients."""
    return await posts_service.feed(db, user, tab=tab, cursor=cursor, limit=limit)


@router.get(
    "/users/{username}/posts",
    response_model=FeedRead,
    summary="A collector's posts (privacy-gated)",
    dependencies=[Depends(feed_limit)],
)
async def get_user_posts(
    username: str,
    cursor: str | None = Query(None),
    limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> FeedRead:
    return await posts_service.user_feed(db, user, username, cursor=cursor, limit=limit)


# ── Post images (public, cache-friendly — same rule as avatars) ──
#
# REGISTERED BEFORE ``/posts/{post_id}``: FastAPI matches routes in
# declaration order, so with the parameterised route first every request for
# /posts/media/<id> would try to read "media" as a post UUID and 422.


@router.get(
    "/posts/media/{media_id}",
    summary="Post image bytes",
    response_class=Response,
)
async def get_post_media(
    media_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> Response:
    """Public like the avatar endpoint: an ``<img>`` tag carries no auth on
    any of our clients. The URL is unguessable, and the *post* around it
    stays behind the privacy gate."""
    row = await db.get(SocialPostMedia, media_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Image not found")
    body = await post_media.load(media_id)
    if body is None:
        raise HTTPException(status_code=404, detail="Image not found")
    # A media row's bytes are written once and never replaced (see
    # post_media), so this can be cached forever.
    return Response(
        content=body,
        media_type=row.content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ── Posts ──


@router.post(
    "/posts",
    response_model=PostRead,
    status_code=201,
    summary="Publish a post",
    dependencies=[Depends(post_limit)],
)
async def create_post(
    body: str | None = Form(
        None,
        max_length=MAX_POST_BODY,
        description="Caption. #tags and @mentions in this text are indexed on write.",
    ),
    card_id: uuid.UUID | None = Form(
        None, description="Optional catalog card this post showcases."
    ),
    images: list[UploadFile] = File(
        [], description="Up to 4 JPEG/PNG/WebP images, 12 MB each."
    ),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> PostRead:
    """Multipart, so text and photos arrive in ONE request — a two-step
    "create then attach" leaves a half-made post behind whenever the second
    call fails."""
    payloads: list[posts_service.NewImage] = []
    for image in images:
        # An empty file part is what a browser sends for an untouched file
        # input; treat it as "no image" rather than an error.
        if not image.filename:
            continue
        content_type = (image.content_type or "").lower()
        if content_type not in post_media.ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=415, detail="Photos must be JPEG, PNG or WebP"
            )
        data = await image.read()
        if len(data) > post_media.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Image too large (max 12 MB)")
        if data:
            payloads.append(
                posts_service.NewImage(body=data, content_type=content_type)
            )
    return await posts_service.create_post(
        db, user, body=body, images=payloads, card_id=card_id
    )


@router.get("/posts/{post_id}", response_model=PostRead, summary="One post")
async def get_post(
    post_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> PostRead:
    return await posts_service.get_post(db, user, post_id)


@router.delete("/posts/{post_id}", status_code=204, summary="Delete a post")
async def delete_post(
    post_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await posts_service.delete_post(db, user, post_id)


@router.post(
    "/posts/{post_id}/like",
    response_model=PostLikeRead,
    summary="Like a post",
    dependencies=[Depends(reaction_limit)],
)
async def like_post(
    post_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> PostLikeRead:
    return await posts_service.like_post(db, user, post_id)


@router.delete(
    "/posts/{post_id}/like",
    response_model=PostLikeRead,
    summary="Unlike a post",
    dependencies=[Depends(reaction_limit)],
)
async def unlike_post(
    post_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> PostLikeRead:
    return await posts_service.unlike_post(db, user, post_id)


# ── Comments ──


@router.get(
    "/posts/{post_id}/comments",
    response_model=CommentThreadRead,
    summary="A post's comment thread",
    dependencies=[Depends(feed_limit)],
)
async def list_comments(
    post_id: uuid.UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> CommentThreadRead:
    """Top-level comments oldest first, each carrying its first two replies."""
    return await comments_service.list_comments(
        db, user, post_id, offset=offset, limit=limit
    )


@router.get(
    "/comments/{comment_id}/replies",
    response_model=CommentThreadRead,
    summary="The rest of a comment's replies",
    dependencies=[Depends(feed_limit)],
)
async def list_replies(
    comment_id: uuid.UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> CommentThreadRead:
    return await comments_service.list_replies(
        db, user, comment_id, offset=offset, limit=limit
    )


@router.post(
    "/posts/{post_id}/comments",
    response_model=CommentRead,
    status_code=201,
    summary="Comment on a post (or reply to a comment)",
    dependencies=[Depends(comment_limit)],
)
async def add_comment(
    post_id: uuid.UUID,
    payload: CommentCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> CommentRead:
    return await comments_service.add_comment(
        db, user, post_id, body=payload.body, parent_id=payload.parent_id
    )


@router.delete("/comments/{comment_id}", status_code=204, summary="Delete a comment")
async def delete_comment(
    comment_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await comments_service.delete_comment(db, user, comment_id)


@router.post(
    "/comments/{comment_id}/like",
    response_model=PostLikeRead,
    summary="Like a comment",
    dependencies=[Depends(reaction_limit)],
)
async def like_comment(
    comment_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> PostLikeRead:
    return await comments_service.like_comment(db, user, comment_id, liked=True)


@router.delete(
    "/comments/{comment_id}/like",
    response_model=PostLikeRead,
    summary="Unlike a comment",
    dependencies=[Depends(reaction_limit)],
)
async def unlike_comment(
    comment_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> PostLikeRead:
    return await comments_service.like_comment(db, user, comment_id, liked=False)


# ── Hashtags + unified search ──


@router.get(
    "/hashtags/trending",
    response_model=list[HashtagRead],
    summary="Trending tags (the chip row)",
    dependencies=[Depends(feed_limit)],
)
async def trending_hashtags(
    limit: int = Query(12, ge=1, le=30),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[HashtagRead]:
    return await hashtags_service.trending(db, user, limit=limit)


@router.get(
    "/hashtags/suggest",
    response_model=list[HashtagRead],
    summary="Tag autocomplete for the composer",
    dependencies=[Depends(feed_limit)],
)
async def suggest_hashtags(
    q: str = Query("", max_length=64, description="What's been typed after '#'."),
    limit: int = Query(8, ge=1, le=20),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[HashtagRead]:
    """What to offer while someone is typing a `#`.

    An empty query returns TRENDING rather than nothing: the moment the
    '#' is typed is exactly when a suggestion is most useful, and showing
    an empty list there teaches people the feature doesn't work.

    REGISTERED BEFORE `/hashtags/{tag}/posts` — FastAPI matches in order,
    so with the parameterised route first "suggest" would be read as a tag.
    """
    if not q.strip():
        return await hashtags_service.trending(db, user, limit=limit)
    return await hashtags_service.search(db, user, q, limit=limit)


@router.get(
    "/hashtags/{tag}/posts",
    response_model=FeedRead,
    summary="Posts carrying a tag",
    dependencies=[Depends(feed_limit)],
)
async def hashtag_posts(
    tag: str,
    cursor: str | None = Query(None),
    limit: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> FeedRead:
    return await hashtags_service.tag_feed(db, user, tag, cursor=cursor, limit=limit)


# ── Safety ──


@router.get(
    "/report-reasons",
    response_model=dict[str, str],
    summary="The reasons a report can carry",
)
async def report_reasons(
    user: User = Depends(require_user),
) -> dict[str, str]:
    """Server-owned so both clients offer the SAME closed list — a reason
    one client can send and the other can't is a reason nobody counts."""
    return dict(safety.REPORT_REASONS)


@router.post(
    "/reports",
    response_model=ModerationCaseRead,
    status_code=201,
    summary="Report a post, comment or profile",
    dependencies=[Depends(comment_limit)],
)
async def create_report(
    payload: ReportCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ModerationCaseRead:
    """Reporting the same thing twice is idempotent, not a second vote."""
    return await safety.report(db, user, payload)


@router.get(
    "/search/all",
    response_model=SocialSearchRead,
    summary="Search users AND hashtags in one call",
    dependencies=[Depends(feed_limit)],
)
async def search_all(
    q: str = Query(..., min_length=2, max_length=60),
    limit: int = Query(10, ge=1, le=25),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> SocialSearchRead:
    """One request behind the feed's search bar. Composing both halves here
    keeps their relative ranking a single decision rather than something
    each client re-invents."""
    return SocialSearchRead(
        users=await service.search(db, user, q, limit),
        hashtags=await hashtags_service.search(db, user, q, limit=limit),
    )


__all__ = ["router"]
