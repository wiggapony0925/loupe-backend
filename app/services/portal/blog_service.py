"""Blog CRUD — public reads (published only) + admin management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.blog import BlogPost
from app.models.enums import BlogStatusEnum
from app.schemas.portal import BlogPostCreate, BlogPostUpdate, slugify
from app.utils.logger import get_logger

logger = get_logger("services.blog")


async def _unique_slug(
    db: AsyncSession, base: str, *, exclude_id: uuid.UUID | None = None
) -> str:
    """Return `base`, suffixing -2, -3, … until it's unique."""
    base = slugify(base)
    candidate = base
    n = 1
    while True:
        stmt = select(BlogPost.id).where(BlogPost.slug == candidate)
        if exclude_id is not None:
            stmt = stmt.where(BlogPost.id != exclude_id)
        if (await db.execute(stmt)).first() is None:
            return candidate
        n += 1
        candidate = f"{base}-{n}"


async def list_published(
    db: AsyncSession, *, limit: int = 50, offset: int = 0
) -> list[BlogPost]:
    rows = (
        (
            await db.execute(
                select(BlogPost)
                .where(BlogPost.status == BlogStatusEnum.published.value)
                .order_by(
                    BlogPost.published_at.desc().nullslast(), BlogPost.created_at.desc()
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def get_published_by_slug(db: AsyncSession, slug: str) -> BlogPost:
    row = (
        await db.execute(
            select(BlogPost).where(
                BlogPost.slug == slug,
                BlogPost.status == BlogStatusEnum.published.value,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Post not found"
        )
    return row


async def admin_list(db: AsyncSession) -> list[BlogPost]:
    rows = (
        (await db.execute(select(BlogPost).order_by(BlogPost.created_at.desc())))
        .scalars()
        .all()
    )
    return list(rows)


async def admin_get(db: AsyncSession, post_id: uuid.UUID) -> BlogPost:
    row = (
        await db.execute(select(BlogPost).where(BlogPost.id == post_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Post not found"
        )
    return row


async def create(db: AsyncSession, payload: BlogPostCreate) -> BlogPost:
    slug = await _unique_slug(db, payload.slug or payload.title)
    published_at = (
        datetime.now(UTC) if payload.status == BlogStatusEnum.published else None
    )
    row = BlogPost(
        slug=slug,
        title=payload.title,
        excerpt=payload.excerpt,
        body=payload.body,
        tag=payload.tag,
        author=payload.author,
        cover_image_url=payload.cover_image_url,
        read_minutes=payload.read_minutes,
        status=payload.status.value,
        published_at=published_at,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    if row.status == BlogStatusEnum.published.value:
        await announce_published(db, row)
    return row


async def announce_published(db: AsyncSession, row: BlogPost) -> int:
    """Put a newly published article in every user's inbox (and on their phone).

    Publishing used to reach nobody: the mobile inbox derived a "news" item by
    polling the public post list, so an article only appeared if the user
    happened to open the app and refresh. Now it's a real notification per
    user, keyed ``blog:<post_id>`` so a post that's edited, unpublished and
    republished can still only ever announce itself once.

    Never raises — a notification failure must not fail the publish.
    """
    from app.services import notification_service

    try:
        return await notification_service.broadcast(
            db,
            category="news",
            kind="blog_post",
            title=row.title,
            body=row.excerpt,
            href=f"/blog/{row.slug}",
            image_url=row.cover_image_url,
            dedupe_prefix=f"blog:{row.id}",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("blog announce failed post=%s (%s)", row.id, exc)
        return 0


async def update(
    db: AsyncSession, post_id: uuid.UUID, payload: BlogPostUpdate
) -> BlogPost:
    row = await admin_get(db, post_id)
    data = payload.model_dump(exclude_unset=True)
    just_published = False

    if data.get("slug"):
        row.slug = await _unique_slug(db, data.pop("slug"), exclude_id=row.id)
    else:
        data.pop("slug", None)

    if "status" in data and data["status"] is not None:
        new_status = data["status"]
        row.status = (
            new_status.value
            if isinstance(new_status, BlogStatusEnum)
            else str(new_status)
        )
        # First publish stamps published_at; never clobber an existing one.
        if row.status == BlogStatusEnum.published.value and row.published_at is None:
            row.published_at = datetime.now(UTC)
            just_published = True
        data.pop("status")

    for key, value in data.items():
        setattr(row, key, value)

    await db.commit()
    await db.refresh(row)
    if just_published:
        await announce_published(db, row)
    return row


async def delete(db: AsyncSession, post_id: uuid.UUID) -> None:
    row = await admin_get(db, post_id)
    await db.delete(row)
    await db.commit()


__all__ = [
    "admin_get",
    "admin_list",
    "create",
    "delete",
    "get_published_by_slug",
    "list_published",
    "update",
]
