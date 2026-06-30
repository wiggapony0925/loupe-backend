"""Admin card-data lineage (`/v1/admin/card-tree`).

Exposes the catalog provider graph + ordered price-fallback chain that powers
the unified Card / Set model, for the developer-portal "Card Tree" visualization.
Pure metadata (reads the declarative lineage + live provider config) — no DB.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.integrations._http import apitcg
from app.services.catalog import card_lineage

router = APIRouter(prefix="/card-tree", tags=["admin-card-tree"])


@router.get("", summary="Card/Set data lineage + price fallback chain")
async def get_card_tree() -> dict[str, Any]:
    tree = card_lineage.build_card_tree()
    # Live monthly request budgets for metered catalog providers — shows the
    # cost-control (cache-first) strategy working. apitcg's free tier is 1000/mo;
    # browse/search/detail are served from the Redis-cached catalog, so this
    # should stay tiny no matter how many users we serve.
    tree["budgets"] = [await apitcg.budget.usage()]
    return tree


__all__ = ["router"]
