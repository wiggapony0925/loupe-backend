"""Notification wire schemas — the inbox contract both clients render."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.notification import CATEGORIES


class NotificationRead(BaseModel):
    """One inbox row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category: str = Field(..., description="market | news | social | billing | system")
    kind: str = Field(..., description="Specific event, e.g. price_alert, blog_post.")
    title: str
    body: str | None = None
    href: str | None = Field(
        None, description="In-app path to open on tap. Not a URL — clients resolve it."
    )
    image_url: str | None = None
    data: dict[str, Any] | None = None
    read_at: datetime | None = None
    created_at: datetime

    @property
    def unread(self) -> bool:
        return self.read_at is None


class NotificationPage(BaseModel):
    """A page of the inbox plus the badge count.

    ``unread`` is the total across the whole inbox, not this page — the badge
    must not change just because the user scrolled.
    """

    items: list[NotificationRead] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 25
    unread: int = 0


class NotificationCategoryRead(BaseModel):
    """One tab of the inbox filter strip, as the server defines it."""

    key: str
    label: str
    description: str
    icon: str
    tone: str
    unread: int


class NotificationSummaryRead(BaseModel):
    """Everything needed to draw the inbox header in one request."""

    unread: int
    categories: list[NotificationCategoryRead]


class MarkReadRequest(BaseModel):
    """Mark specific notifications read, or all of them."""

    ids: list[uuid.UUID] = Field(
        default_factory=list, description="Ignored when `all` is true."
    )
    all: bool = Field(False, description="Clear the entire badge.")


class UnreadCountRead(BaseModel):
    unread: int = 0


class AdminNotificationCreate(BaseModel):
    """Admin composer payload — one user or a broadcast."""

    title: str = Field(..., min_length=1, max_length=200)
    body: str | None = Field(None, max_length=2000)
    category: str = Field(
        "news", description=f"One of: {', '.join(sorted(CATEGORIES))}"
    )
    href: str | None = Field(
        None, max_length=500, description="In-app path, e.g. /app/vault."
    )
    image_url: str | None = Field(None, max_length=1000)
    #: Omit to broadcast to every active user; set to notify one person.
    user_id: uuid.UUID | None = None
    #: When false the row is created but no device is buzzed — useful for
    #: notices that belong in the inbox without interrupting anyone.
    push: bool = True
    #: Preview mode: render the audience count without writing anything.
    dry_run: bool = False


class AdminNotificationResult(BaseModel):
    created: int = 0
    audience: int = 0
    dry_run: bool = False


__all__ = [
    "AdminNotificationCreate",
    "AdminNotificationResult",
    "MarkReadRequest",
    "NotificationPage",
    "NotificationRead",
    "UnreadCountRead",
]
