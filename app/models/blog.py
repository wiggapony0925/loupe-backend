"""Blog ORM model — editorial posts shown on the public blog and managed
from the admin developer portal.

``body`` holds the article as plain text / lightweight markup; ``status``
is stored as the enum value (see :class:`app.models.enums.BlogStatusEnum`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol
from app.models.enums import BlogStatusEnum


class BlogPost(Base):
    """A single blog article."""

    __tablename__ = "blog_posts"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(
        String(200), unique=True, index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    excerpt: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    body: Mapped[str] = mapped_column(Text(), default="", nullable=False)
    tag: Mapped[str] = mapped_column(String(60), default="Update", nullable=False)
    author: Mapped[str] = mapped_column(
        String(120), default="The Loupe Team", nullable=False
    )
    cover_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    read_minutes: Mapped[int] = mapped_column(default=3, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=BlogStatusEnum.draft.value, index=True, nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
        Index("ix_blog_posts_status_published", "status", "published_at"),
    )


__all__ = ["BlogPost"]
