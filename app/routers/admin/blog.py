"""Admin blog management (`/v1/admin/blog`)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.enums import BlogStatusEnum
from app.models.user import User
from app.schemas.portal import BlogPostCreate, BlogPostRead, BlogPostUpdate
from app.services import audit_service, email_service
from app.services.auth import unsubscribe_service, user_service
from app.services.portal import blog_service

router = APIRouter(prefix="/blog", tags=["admin-blog"])

_PUBLISHED = BlogStatusEnum.published.value


async def _announce(db: AsyncSession, background: BackgroundTasks, post) -> None:
    """Queue a 'new post' email to subscribed users (best-effort, after response).

    Announcement-class mail: recipients honor the per-user opt-out and each
    message gets that user's signed one-click unsubscribe link.
    """
    recipients = [
        (email, unsubscribe_service.unsubscribe_url(uid))
        for uid, email in await user_service.announcement_recipients(db)
    ]
    if recipients:
        background.add_task(
            email_service.send_blog_announcement,
            recipients,
            title=post.title,
            excerpt=post.excerpt,
            slug=post.slug,
            cover_image_url=post.cover_image_url,
            tag=post.tag,
            author=post.author,
            read_minutes=post.read_minutes,
        )


@router.get("", response_model=list[BlogPostRead], summary="List all blog posts")
async def list_posts(db: AsyncSession = Depends(get_db)) -> list[BlogPostRead]:
    rows = await blog_service.admin_list(db)
    return [BlogPostRead.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=BlogPostRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a blog post",
)
async def create_post(
    payload: BlogPostCreate,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> BlogPostRead:
    row = await blog_service.create(db, payload)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="blog.create",
        target_table="blog_posts",
        target_id=row.id,
        payload={"title": row.title, "status": row.status},
    )
    if row.status == _PUBLISHED:
        await _announce(db, background, row)
    return BlogPostRead.model_validate(row)


@router.get("/{post_id}", response_model=BlogPostRead, summary="Get a blog post")
async def get_post(
    post_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> BlogPostRead:
    row = await blog_service.admin_get(db, post_id)
    return BlogPostRead.model_validate(row)


@router.patch("/{post_id}", response_model=BlogPostRead, summary="Update a blog post")
async def update_post(
    post_id: uuid.UUID,
    payload: BlogPostUpdate,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> BlogPostRead:
    # Capture publish state *before* the update so we only announce on a post's
    # first publish (never on edits to an already-live post, and never again
    # once it's been unpublished and republished). `published_at` is stamped on
    # that first publish and never cleared, so it is the same "has this post
    # already announced itself?" key the in-app notification dedupes on
    # (`blog:<id>`) — the two must not disagree, or a republish mails the whole
    # announcement list a second time while creating no new inbox item.
    before = await blog_service.admin_get(db, post_id)
    already_announced = before.published_at is not None
    row = await blog_service.update(db, post_id, payload)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="blog.update",
        target_table="blog_posts",
        target_id=row.id,
        payload=payload.model_dump(exclude_unset=True, mode="json"),
    )
    if not already_announced and row.status == _PUBLISHED:
        await _announce(db, background, row)
    return BlogPostRead.model_validate(row)


@router.delete(
    "/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a blog post",
)
async def delete_post(
    post_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> None:
    await blog_service.delete(db, post_id)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="blog.delete",
        target_table="blog_posts",
        target_id=post_id,
    )


__all__ = ["router"]
