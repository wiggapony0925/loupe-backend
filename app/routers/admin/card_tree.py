"""Admin card-data lineage (`/v1/admin/card-tree`).

Exposes the catalog provider graph + ordered price-fallback chain that powers
the unified Card / Set model, for the developer-portal "Card Tree" visualization.
Pure metadata (reads the declarative lineage + live provider config) — no DB.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.services.catalog import card_lineage

router = APIRouter(prefix="/card-tree", tags=["admin-card-tree"])


@router.get("", summary="Card/Set data lineage + price fallback chain")
async def get_card_tree() -> dict[str, Any]:
    return card_lineage.build_card_tree()


__all__ = ["router"]
