"""Collection (binder/deck) operations.

Pure CRUD over :class:`Collection` and its :class:`CollectionItem`
join rows. Extracted from :mod:`app.routers.collection.collections` so
the router stays a thin HTTP shell. All callers must already have an
authenticated :class:`User`; ownership is enforced here by joining on
``user_id``.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.collection import Collection, CollectionItem
from app.models.grade import GradedCard
from app.models.user import User
from app.schemas.collection import (
    CollectionCreate,
    CollectionSummary,
    CollectionUpdate,
)
from app.social.services import safety


def holdings_scope(
    collection_id: uuid.UUID | None, user: User
) -> ColumnElement[bool] | None:
    """A reusable ``WHERE`` fragment scoping ``GradedCard`` rows to one
    collection — the single seam every value surface (dashboard, analytics,
    vault list, statement PDF) uses so the *active collection* consistently
    scopes them all, backend-side.

    ``None`` ⇒ the "All" view (no scoping). Ownership-safe: the subquery only
    matches items in a collection owned by ``user``, so a foreign / unknown
    collection id yields an empty scope instead of leaking anyone's holdings.
    """
    if collection_id is None:
        return None
    return GradedCard.id.in_(
        select(CollectionItem.graded_card_id)
        .join(Collection, Collection.id == CollectionItem.collection_id)
        .where(
            CollectionItem.collection_id == collection_id,
            Collection.user_id == user.id,
        )
    )


async def get_owned(
    db: AsyncSession, user: User, collection_id: uuid.UUID
) -> Collection:
    """Return the collection or 404 if it isn't owned by ``user``."""
    row = (
        await db.execute(
            select(Collection).where(
                Collection.id == collection_id, Collection.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    return row


async def list_for_user(db: AsyncSession, user: User) -> list[Collection]:
    return list(
        (
            await db.execute(
                select(Collection)
                .where(Collection.user_id == user.id)
                .order_by(Collection.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def create(db: AsyncSession, user: User, payload: CollectionCreate) -> Collection:
    # Collection names and descriptions surface on a collector's PUBLIC
    # profile (see SocialPortfolioRead), so they are user-authored text other
    # people read — same chokepoint as a post.
    await _screen(db, user, payload.name, payload.description)
    row = Collection(
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        color=payload.color,
        is_public=payload.is_public,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update(
    db: AsyncSession,
    user: User,
    collection_id: uuid.UUID,
    payload: CollectionUpdate,
) -> Collection:
    row = await get_owned(db, user, collection_id)
    await _screen(db, user, payload.name, payload.description, target_id=collection_id)
    if payload.name is not None:
        row.name = payload.name
    if payload.description is not None:
        row.description = payload.description
    if payload.color is not None:
        row.color = payload.color
    if payload.is_public is not None:
        row.is_public = payload.is_public
    await db.commit()
    await db.refresh(row)
    return row


async def _screen(
    db: AsyncSession,
    user: User,
    name: str | None,
    description: str | None,
    *,
    target_id: uuid.UUID | None = None,
) -> None:
    """Screen a binder's public-facing text. No-op when there's none."""
    text = "\n".join(part for part in (name or "", description or "") if part).strip()
    if not text:
        return
    await safety.enforce(
        db,
        actor=user,
        surface=safety.TARGET_COLLECTION,
        target_id=target_id or uuid.uuid4(),
        text=text,
        excerpt=f"collection: {text}",
        refusal="That collection name looks like it breaks the community rules.",
    )


async def delete(db: AsyncSession, user: User, collection_id: uuid.UUID) -> None:
    row = await get_owned(db, user, collection_id)
    await db.delete(row)
    await db.commit()


async def list_items(
    db: AsyncSession, user: User, collection_id: uuid.UUID
) -> list[GradedCard]:
    await get_owned(db, user, collection_id)
    return list(
        (
            await db.execute(
                select(GradedCard)
                .join(CollectionItem, CollectionItem.graded_card_id == GradedCard.id)
                .where(
                    CollectionItem.collection_id == collection_id,
                    GradedCard.deleted_at.is_(None),
                )
                .order_by(CollectionItem.added_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def add_item(
    db: AsyncSession,
    user: User,
    collection_id: uuid.UUID,
    graded_card_id: uuid.UUID,
) -> None:
    """Add ``graded_card_id`` to ``collection_id``; idempotent."""
    await get_owned(db, user, collection_id)
    graded = (
        await db.execute(
            select(GradedCard).where(
                GradedCard.id == graded_card_id,
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if graded is None:
        raise HTTPException(status_code=404, detail="Graded card not found")
    existing = (
        await db.execute(
            select(CollectionItem).where(
                CollectionItem.collection_id == collection_id,
                CollectionItem.graded_card_id == graded_card_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            CollectionItem(collection_id=collection_id, graded_card_id=graded_card_id)
        )
        await db.commit()


async def remove_item(
    db: AsyncSession,
    user: User,
    collection_id: uuid.UUID,
    graded_card_id: uuid.UUID,
) -> None:
    await get_owned(db, user, collection_id)
    row = (
        await db.execute(
            select(CollectionItem).where(
                CollectionItem.collection_id == collection_id,
                CollectionItem.graded_card_id == graded_card_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Item not in collection")
    await db.delete(row)
    await db.commit()


async def _owned_grade_ids(
    db: AsyncSession, user: User, ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """O(1) lookup set of holdings the user actually owns (filters fakes)."""
    if not ids:
        return set()
    rows = (
        (
            await db.execute(
                select(GradedCard.id).where(
                    GradedCard.user_id == user.id,
                    GradedCard.deleted_at.is_(None),
                    GradedCard.id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def bulk_add_items(
    db: AsyncSession,
    user: User,
    collection_id: uuid.UUID,
    graded_card_ids: list[uuid.UUID],
) -> int:
    """Add many holdings to a collection in one round-trip. Idempotent.

    Complexity: O(n) with n = len(ids), capped by the schema (≤200).
    """
    await get_owned(db, user, collection_id)
    # De-dupe while preserving order — O(n).
    unique: list[uuid.UUID] = list(dict.fromkeys(graded_card_ids))
    owned = await _owned_grade_ids(db, user, unique)
    if not owned:
        return 0
    existing = set(
        (
            await db.execute(
                select(CollectionItem.graded_card_id).where(
                    CollectionItem.collection_id == collection_id,
                    CollectionItem.graded_card_id.in_(owned),
                )
            )
        )
        .scalars()
        .all()
    )
    to_add = [gid for gid in unique if gid in owned and gid not in existing]
    for gid in to_add:
        db.add(CollectionItem(collection_id=collection_id, graded_card_id=gid))
    if to_add:
        await db.commit()
    return len(to_add)


async def bulk_remove_items(
    db: AsyncSession,
    user: User,
    collection_id: uuid.UUID,
    graded_card_ids: list[uuid.UUID],
) -> int:
    """Remove many holdings from a collection. Missing memberships are no-ops."""
    await get_owned(db, user, collection_id)
    unique = list(dict.fromkeys(graded_card_ids))
    rows = list(
        (
            await db.execute(
                select(CollectionItem).where(
                    CollectionItem.collection_id == collection_id,
                    CollectionItem.graded_card_id.in_(unique),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await db.delete(row)
    if rows:
        await db.commit()
    return len(rows)


async def transfer_items(
    db: AsyncSession,
    user: User,
    target_id: uuid.UUID,
    source_id: uuid.UUID,
    graded_card_ids: list[uuid.UUID],
) -> tuple[int, int]:
    """Move holdings from ``source`` → ``target`` (add then remove).

    Returns ``(added, removed)``. Holdings themselves are never deleted —
    only the categorization changes.
    """
    if target_id == source_id:
        raise HTTPException(
            status_code=400, detail="Cannot transfer a collection into itself"
        )
    added = await bulk_add_items(db, user, target_id, graded_card_ids)
    removed = await bulk_remove_items(db, user, source_id, graded_card_ids)
    return added, removed


async def overview(db: AsyncSession, user: User) -> list[CollectionSummary]:
    """The portfolio-switcher list: a synthetic **All** (everything owned,
    undeletable) followed by each custom collection, all with a live card count
    and total value. This is exactly what the dashboard dropdown renders — the
    frontend just displays it."""
    # Count + value per collection in one grouped query.
    per_rows = (
        await db.execute(
            select(
                CollectionItem.collection_id,
                func.count(GradedCard.id),
                func.coalesce(func.sum(GradedCard.estimated_value_usd), 0),
            )
            .join(GradedCard, GradedCard.id == CollectionItem.graded_card_id)
            .join(Collection, Collection.id == CollectionItem.collection_id)
            .where(Collection.user_id == user.id, GradedCard.deleted_at.is_(None))
            .group_by(CollectionItem.collection_id)
        )
    ).all()
    stats = {cid: (int(n), float(v or 0)) for (cid, n, v) in per_rows}

    # "All" totals over the whole vault.
    all_row = (
        await db.execute(
            select(
                func.count(GradedCard.id),
                func.coalesce(func.sum(GradedCard.estimated_value_usd), 0),
            ).where(GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None))
        )
    ).one()

    out = [
        CollectionSummary(
            id=None,
            name="All",
            color=None,
            card_count=int(all_row[0]),
            total_value_usd=float(all_row[1] or 0),
            is_all=True,
            deletable=False,
        )
    ]
    for c in await list_for_user(db, user):
        count, value = stats.get(c.id, (0, 0.0))
        out.append(
            CollectionSummary(
                id=c.id,
                name=c.name,
                color=c.color,
                card_count=count,
                total_value_usd=value,
                is_all=False,
                deletable=True,
            )
        )
    return out


async def merge(
    db: AsyncSession, user: User, target_id: uuid.UUID, source_id: uuid.UUID
) -> None:
    """Fold ``source`` into ``target`` — move its items over (de-duped) then
    delete the now-empty source collection. Holdings are never touched; only the
    categorization is combined."""
    if target_id == source_id:
        raise HTTPException(
            status_code=400, detail="Cannot merge a collection into itself"
        )
    target = await get_owned(db, user, target_id)
    source = await get_owned(db, user, source_id)

    in_target = set(
        (
            await db.execute(
                select(CollectionItem.graded_card_id).where(
                    CollectionItem.collection_id == target.id
                )
            )
        )
        .scalars()
        .all()
    )
    source_items = (
        (
            await db.execute(
                select(CollectionItem).where(CollectionItem.collection_id == source.id)
            )
        )
        .scalars()
        .all()
    )
    for item in source_items:
        if item.graded_card_id in in_target:
            await db.delete(item)  # already in target → drop the duplicate link
        else:
            item.collection_id = target.id  # move the categorization over
    await db.delete(source)
    await db.commit()


__all__ = [
    "add_item",
    "create",
    "delete",
    "get_owned",
    "list_for_user",
    "list_items",
    "merge",
    "overview",
    "remove_item",
    "update",
]
