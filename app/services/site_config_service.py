"""Read/write the singleton :class:`SiteConfig`.

``get`` lazily creates the row with defaults, so a create-all dev DB and a
migrated prod DB behave identically. Everything that needs the plan shape or
the announcement goes through here.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.site_config import SiteConfig
from app.schemas.site_config import (
    AnnouncementRead,
    AnnouncementUpdate,
    PlanConfigRead,
    PlanConfigUpdate,
    SiteConfigRead,
)


async def get(db: AsyncSession) -> SiteConfig:
    """The singleton config row, created with defaults on first access."""
    row = (await db.execute(select(SiteConfig).limit(1))).scalar_one_or_none()
    if row is not None:
        return row
    row = SiteConfig()
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent request created it first — re-read.
        await db.rollback()
        row = (await db.execute(select(SiteConfig).limit(1))).scalar_one()
        return row
    await db.refresh(row)
    return row


def to_read(cfg: SiteConfig) -> SiteConfigRead:
    return SiteConfigRead(
        plan=PlanConfigRead.model_validate(cfg),
        announcement=AnnouncementRead(
            enabled=cfg.announcement_enabled,
            message=cfg.announcement_message,
            tone=cfg.announcement_tone,
            cta_label=cfg.announcement_cta_label,
            cta_href=cfg.announcement_cta_href,
        ),
        updated_at=cfg.updated_at,
    )


async def update_plan(db: AsyncSession, payload: PlanConfigUpdate) -> SiteConfig:
    cfg = await get(db)
    data = payload.model_dump(exclude_unset=True)
    data.pop("clear_card_limit", None)
    data.pop("clear_statement_limit", None)
    for key, value in data.items():
        setattr(cfg, key, value)
    # Explicit "make it unlimited" requests (null can't be expressed otherwise).
    if payload.clear_card_limit:
        cfg.free_card_limit = None
    if payload.clear_statement_limit:
        cfg.free_statement_limit = None
    await db.commit()
    await db.refresh(cfg)
    return cfg


async def update_announcement(
    db: AsyncSession, payload: AnnouncementUpdate
) -> SiteConfig:
    cfg = await get(db)
    data = payload.model_dump(exclude_unset=True)
    field_map = {
        "enabled": "announcement_enabled",
        "message": "announcement_message",
        "tone": "announcement_tone",
        "cta_label": "announcement_cta_label",
        "cta_href": "announcement_cta_href",
    }
    for key, value in data.items():
        setattr(cfg, field_map[key], value)
    await db.commit()
    await db.refresh(cfg)
    return cfg


async def public_announcement(db: AsyncSession) -> AnnouncementRead:
    """What every client polls — empty/disabled unless an admin turned it on."""
    cfg = await get(db)
    if not cfg.announcement_enabled or not cfg.announcement_message.strip():
        return AnnouncementRead(enabled=False, message="", tone="info")
    return AnnouncementRead(
        enabled=True,
        message=cfg.announcement_message,
        tone=cfg.announcement_tone,
        cta_label=cfg.announcement_cta_label,
        cta_href=cfg.announcement_cta_href,
    )


__all__ = [
    "get",
    "public_announcement",
    "to_read",
    "update_announcement",
    "update_plan",
]
