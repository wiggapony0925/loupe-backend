"""Admin blog management (`/v1/admin/blog`)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.schemas.portal import BlogPostCreate, BlogPostRead, BlogPostUpdate
from app.services import audit_service
from app.services.portal import blog_service

router = APIRouter(prefix="/blog", tags=["admin-blog"])


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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> BlogPostRead:
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
