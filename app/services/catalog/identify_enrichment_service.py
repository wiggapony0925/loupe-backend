"""Identify-candidate enrichment — pricing + ownership, composed server-side.

A scan result is only actionable when the user can see, per candidate:
  • what the card is WORTH right now (mirror market price), and
  • whether they ALREADY OWN it — how many copies, and whether any are
    graded slabs.

Both signals live in the backend (catalog mirror + graded_cards), so this
runs as one query batch per identify call and every client — web, mobile,
native scanner — renders identical numbers with zero client-side math.

Best effort: enrichment failures never break identification; candidates
simply come back without the extra fields.
"""

from __future__ import annotations

import uuid

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card_external_ref import CardExternalRef
from app.models.catalog_mirror import CatalogMirrorCard
from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from app.models.user import User
from app.schemas.identification import IdentifyCandidate
from app.utils.logger import get_logger

logger = get_logger("services.identify_enrichment")


async def enrich_candidates(
    db: AsyncSession,
    user: User | None,
    candidates: list[IdentifyCandidate],
) -> None:
    """Mutate *candidates* in place with market price + ownership."""
    if not candidates:
        return
    try:
        await _enrich(db, user, candidates)
    except Exception:  # pragma: no cover — enrichment is always optional
        logger.warning("identify enrichment failed", exc_info=True)


async def _enrich(
    db: AsyncSession,
    user: User | None,
    candidates: list[IdentifyCandidate],
) -> None:
    upstream_ids = {c.upstream_id for c in candidates if c.upstream_id}
    local_ids = {c.card_id for c in candidates if c.card_id}

    # ── Market price: catalog mirror keyed by the composite upstream id. ──
    prices: dict[str, float] = {}
    if upstream_ids:
        rows = (
            await db.execute(
                select(CatalogMirrorCard.id, CatalogMirrorCard.sort_price).where(
                    CatalogMirrorCard.id.in_(upstream_ids)
                )
            )
        ).all()
        prices = {rid: float(p) for rid, p in rows if p is not None}

    # ── upstream → local card mapping (for ownership on unmaterialized
    #    candidates the user may still own via a previously materialized id).
    upstream_to_local: dict[str, str] = {}
    if upstream_ids:
        pairs = [tuple(u.split(":", 1)) for u in upstream_ids if ":" in u]
        if pairs:
            sources = {s for s, _ in pairs}
            externals = {e for _, e in pairs}
            ref_rows = (
                await db.execute(
                    select(
                        CardExternalRef.source,
                        CardExternalRef.external_id,
                        CardExternalRef.card_id,
                    ).where(
                        CardExternalRef.source.in_(sources),
                        CardExternalRef.external_id.in_(externals),
                    )
                )
            ).all()
            wanted = set(pairs)
            for source, external_id, card_id in ref_rows:
                if (source, external_id) in wanted:
                    upstream_to_local[f"{source}:{external_id}"] = str(card_id)
    local_ids |= set(upstream_to_local.values())

    # ── Ownership: copies + graded split for the signed-in user. ──
    # The card_id column is a UUID — compare with UUID objects, not strings.
    local_uuids: set[uuid.UUID] = set()
    for cid in local_ids:
        try:
            local_uuids.add(uuid.UUID(cid))
        except (ValueError, TypeError):
            continue
    owned: dict[str, tuple[int, int]] = {}
    if user is not None and local_uuids:
        grade_rows = (
            await db.execute(
                select(
                    GradedCard.card_id,
                    func.count(GradedCard.id),
                    func.sum(
                        case(
                            (GradedCard.house != GradeHouseEnum.loupe, 1),
                            else_=0,
                        )
                    ),
                )
                .where(
                    GradedCard.user_id == user.id,
                    GradedCard.card_id.in_(local_uuids),
                    GradedCard.deleted_at.is_(None),
                )
                .group_by(GradedCard.card_id)
            )
        ).all()
        owned = {
            str(card_id): (int(copies), int(graded or 0))
            for card_id, copies, graded in grade_rows
        }

    # ── Fill the candidates. ──
    for c in candidates:
        if c.upstream_id and c.upstream_id in prices:
            c.market_price_usd = round(prices[c.upstream_id], 2)
        local = c.card_id or (
            upstream_to_local.get(c.upstream_id) if c.upstream_id else None
        )
        if local and local in owned:
            copies, graded = owned[local]
            c.owned = copies > 0
            c.copies_owned = copies
            c.graded_copies = graded


__all__ = ["enrich_candidates"]
