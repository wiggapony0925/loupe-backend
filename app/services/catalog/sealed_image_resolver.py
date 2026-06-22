"""Resolve TCGplayer catalog data (photos + live prices) for sealed products.

Our ``sealed_products`` are hand-seeded SKUs with no image or price history.
TCGcsv (https://tcgcsv.com) mirrors the full TCGplayer catalog — including
sealed products — with a product ``imageUrl`` and daily low/mid/high/market
prices. We match a ``(tcg, set_name, product_type)`` to the right TCGcsv group
+ product and surface the real photo + current market snapshot, cached
in-process.

Best-effort and gated on ``TCGCSV_ENABLED`` — if the upstream is slow or down,
callers fall back to generated art / MSRP only.
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
        # (category, group_id) -> products [{name, image, id, url}]
        self.products: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self.products_at: dict[tuple[int, int], float] = {}
        # (category, group_id) -> {product_id: {low, mid, high, market}}
        self.prices: dict[tuple[int, int], dict[int, dict[str, float | None]]] = {}
        self.prices_at: dict[tuple[int, int], float] = {}
        self.lock = asyncio.Lock()


_c = _Cache()


def _big(image_url: str) -> str:
    """Upgrade TCGplayer's thumbnail (``_200w.jpg``) to a crisp square."""
    return image_url.replace("_200w.jpg", "_in_1000x1000.jpg")


def _f(v: Any) -> float | None:
    try:
        n = float(v)
        return round(n, 2) if n > 0 else None
    except (TypeError, ValueError):
        return None


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
        {
            "name": (p.get("name") or ""),
            "image": p.get("imageUrl") or "",
            "id": int(p["productId"]) if p.get("productId") is not None else None,
            "url": p.get("url") or "",
        }
        for p in results
        if p.get("name")
    ]
    _c.products_at[key] = time.time()


async def _ensure_prices(category: int, group_id: int) -> None:
    key = (category, group_id)
    if key in _c.prices and time.time() - _c.prices_at.get(key, 0) < _TTL:
        return
    client = await get_http_client()
    resp = await client.get(f"{_BASE}/{category}/{group_id}/prices", timeout=20.0)
    resp.raise_for_status()
    results = (resp.json() or {}).get("results") or []
    out: dict[int, dict[str, float | None]] = {}
    for r in results:
        pid = r.get("productId")
        if pid is None:
            continue
        # First non-empty subtype wins (sealed usually has one "Normal" row).
        out.setdefault(
            int(pid),
            {
                "low": _f(r.get("lowPrice")),
                "mid": _f(r.get("midPrice")),
                "high": _f(r.get("highPrice")),
                "market": _f(r.get("marketPrice")),
            },
        )
    _c.prices[key] = out
    _c.prices_at[key] = time.time()


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


def _pick_product(
    products: list[dict[str, Any]], product_type: SealedProductTypeEnum
) -> dict[str, Any] | None:
    """The canonical TCGplayer product for a type — required tokens present,
    excluded absent, preferring the shortest (most canonical) name."""
    tokens = _TYPE_TOKENS.get(product_type)
    if not tokens:
        return None
    must, never = tokens
    best: tuple[int, dict[str, Any]] | None = None
    for p in products:
        name = p["name"].lower()
        if not all(t in name for t in must):
            continue
        if any(t in name for t in never):
            continue
        cand = (len(name), p)
        if best is None or cand[0] < best[0]:
            best = cand
    return best[1] if best else None


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
        match = _pick_product(_c.products.get((cat, gid), []), p.product_type)
        if match and match.get("image"):
            p.image_url = _big(match["image"])
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


async def resolve_market(product: SealedProduct) -> dict[str, Any] | None:
    """Live TCGplayer market snapshot for one sealed product (best-effort).

    Returns ``{low, mid, high, market, currency, source, marketplace_url,
    image_url}`` or ``None`` when TCGCSV is off / unreachable / unmatched.
    """
    cat = _CATEGORY.get(product.tcg)
    if not get_settings().tcgcsv_enabled or cat is None or not product.set_name:
        return None
    try:
        return await asyncio.wait_for(_resolve_market(product, cat), timeout=9.0)
    except Exception as exc:
        _log.debug("sealed market resolve skipped: %s", exc)
        return None


async def _resolve_market(product: SealedProduct, cat: int) -> dict[str, Any] | None:
    await _ensure_groups(cat)
    gid = _find_group(cat, product.set_name or "")
    if gid is None:
        return None
    await asyncio.gather(
        _ensure_products(cat, gid), _ensure_prices(cat, gid), return_exceptions=True
    )
    match = _pick_product(_c.products.get((cat, gid), []), product.product_type)
    if match is None or match.get("id") is None:
        return None
    price = _c.prices.get((cat, gid), {}).get(int(match["id"]))
    if price is None or price.get("market") is None:
        return None
    return {
        "low": price.get("low"),
        "mid": price.get("mid"),
        "high": price.get("high"),
        "market": price.get("market"),
        "currency": "USD",
        "source": "tcgplayer",
        "marketplace_url": match.get("url") or None,
        "image_url": _big(match["image"]) if match.get("image") else None,
    }


__all__ = ["enrich_images", "resolve_market"]
