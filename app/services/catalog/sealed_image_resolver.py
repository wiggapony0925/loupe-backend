"""Resolve real TCGplayer product photos for our sealed catalog.

Our ``sealed_products`` are hand-seeded SKUs with no image. TCGcsv
(https://tcgcsv.com) mirrors the full TCGplayer catalog — including sealed
products — each with a product ``imageUrl`` on TCGplayer's CDN. We match a
``(tcg, set_name, product_type)`` to the right TCGcsv group + product and
fill ``image_url`` with the real photo, cached in-process.

Best-effort and gated on ``TCGCSV_ENABLED`` — if the upstream is slow or down,
callers just keep the generated cover art (the frontend's ``SealedArt``).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.config import get_settings
from app.integrations.base import get_http_client
from app.models.enums import SealedProductTypeEnum, TcgEnum
from app.models.sealed import SealedProduct
from app.utils.logger import get_logger

_log = get_logger("services.sealed_image")

_BASE = "https://tcgcsv.com/tcgplayer"
# TCGplayer category ids per game.
_CATEGORY: dict[TcgEnum, int] = {
    TcgEnum.pokemon: 3,
    TcgEnum.magic: 1,
    TcgEnum.yugioh: 2,
}
_TTL = 24 * 60 * 60  # TCGcsv refreshes daily.

# product_type → (tokens the TCGplayer name must contain, tokens it must NOT)
# so we pick the canonical SKU (e.g. plain "… Booster Box", not "… Case").
_TYPE_TOKENS: dict[SealedProductTypeEnum, tuple[list[str], list[str]]] = {
    SealedProductTypeEnum.booster_box: (["booster box"], ["bundle", "case", "blister"]),
    SealedProductTypeEnum.etb: (["elite trainer box"], ["case"]),
    SealedProductTypeEnum.booster_pack: (
        ["booster pack"],
        ["bundle", "blister", "art", "box"],
    ),
    SealedProductTypeEnum.bundle: (["booster bundle"], ["box", "case", "lgs"]),
    SealedProductTypeEnum.premium_collection: (["premium collection"], ["case"]),
    SealedProductTypeEnum.tin: (["tin"], ["case"]),
    SealedProductTypeEnum.blister: (["blister"], ["case"]),
    SealedProductTypeEnum.collection_box: (["collection box"], ["premium", "case"]),
    SealedProductTypeEnum.case: (["case"], []),
}


class _Cache:
    def __init__(self) -> None:
        # category -> [(group_id, name_lower)]
        self.groups: dict[int, list[tuple[int, str]]] = {}
        self.groups_at: dict[int, float] = {}
        # (category, group_id) -> products [{name, imageUrl}]
        self.products: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self.products_at: dict[tuple[int, int], float] = {}
        self.lock = asyncio.Lock()


_c = _Cache()


def _big(image_url: str) -> str:
    """Upgrade TCGplayer's thumbnail (``_200w.jpg``) to a crisp square."""
    return image_url.replace("_200w.jpg", "_in_1000x1000.jpg")


async def _ensure_groups(category: int) -> None:
    if category in _c.groups and time.time() - _c.groups_at.get(category, 0) < _TTL:
        return
    client = await get_http_client()
    resp = await client.get(f"{_BASE}/{category}/groups", timeout=20.0)
    resp.raise_for_status()
    results = (resp.json() or {}).get("results") or []
    _c.groups[category] = [
        (int(g["groupId"]), (g.get("name") or "").lower())
        for g in results
        if g.get("groupId") is not None
    ]
    _c.groups_at[category] = time.time()


async def _ensure_products(category: int, group_id: int) -> None:
    key = (category, group_id)
    if key in _c.products and time.time() - _c.products_at.get(key, 0) < _TTL:
        return
    client = await get_http_client()
    resp = await client.get(f"{_BASE}/{category}/{group_id}/products", timeout=20.0)
    resp.raise_for_status()
    results = (resp.json() or {}).get("results") or []
    _c.products[key] = [
        {"name": (p.get("name") or ""), "image": p.get("imageUrl") or ""}
        for p in results
        if p.get("imageUrl")
    ]
    _c.products_at[key] = time.time()


def _find_group(category: int, set_name: str) -> int | None:
    """The TCGcsv group whose name contains the set (e.g. 'SV08: Surging Sparks')."""
    needle = set_name.strip().lower()
    if not needle:
        return None
    best: tuple[int, int] | None = None  # (name_len, group_id) → shortest wins
    for gid, name in _c.groups.get(category, []):
        if needle in name:
            cand = (len(name), gid)
            if best is None or cand < best:
                best = cand
    return best[1] if best else None


def _pick_image(
    products: list[dict[str, Any]], product_type: SealedProductTypeEnum
) -> str | None:
    """Best image for a product type — required tokens present, excluded absent,
    preferring the shortest (most canonical) product name."""
    tokens = _TYPE_TOKENS.get(product_type)
    if not tokens:
        return None
    must, never = tokens
    best: tuple[int, str] | None = None
    for p in products:
        name = p["name"].lower()
        if not all(t in name for t in must):
            continue
        if any(t in name for t in never):
            continue
        cand = (len(name), p["image"])
        if best is None or cand < best:
            best = cand
    return _big(best[1]) if best else None


async def _resolve(targets: list[SealedProduct]) -> bool:
    cats = {p.tcg for p in targets if p.tcg in _CATEGORY}
    await asyncio.gather(
        *[_ensure_groups(_CATEGORY[t]) for t in cats], return_exceptions=True
    )

    plan: list[tuple[SealedProduct, int, int]] = []
    needed: set[tuple[int, int]] = set()
    for p in targets:
        cat = _CATEGORY.get(p.tcg)
        if cat is None or not p.set_name:
            continue
        gid = _find_group(cat, p.set_name)
        if gid is None:
            continue
        plan.append((p, cat, gid))
        needed.add((cat, gid))

    await asyncio.gather(
        *[_ensure_products(cat, gid) for cat, gid in needed], return_exceptions=True
    )

    changed = False
    for p, cat, gid in plan:
        img = _pick_image(_c.products.get((cat, gid), []), p.product_type)
        if img:
            p.image_url = img
            changed = True
    return changed


async def enrich_images(products: list[SealedProduct]) -> bool:
    """Fill ``image_url`` on products that lack one, from TCGplayer (best-effort).

    Returns True if any product was given an image. No-op (returns False) when
    ``TCGCSV_ENABLED`` is off or the upstream is unreachable.
    """
    if not get_settings().tcgcsv_enabled:
        return False
    targets = [
        p for p in products if not p.image_url and p.set_name and p.tcg in _CATEGORY
    ]
    if not targets:
        return False
    try:
        return await asyncio.wait_for(_resolve(targets), timeout=9.0)
    except Exception as exc:  # never let image enrichment break a catalog read
        _log.debug("sealed image enrichment skipped: %s", exc)
        return False


__all__ = ["enrich_images"]
