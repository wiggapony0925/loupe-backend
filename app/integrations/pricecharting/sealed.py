"""PriceCharting sealed-product market resolution.

PriceCharting indexes sealed product unusually cleanly: the console is
"Pokemon <Set>" and the product-name is just the type ("Booster Box", "Elite
Trainer Box", …). A structured ``?q=`` query therefore resolves the exact SKU —
a *more* reliable sealed price source than fuzzy-matching the verbose TCGplayer
catalog names. We use it to fill the gaps TCGplayer misses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.integrations.pricecharting.provider import PriceChartingProvider
from app.models.enums import SealedProductTypeEnum, TcgEnum
from app.utils.logger import get_logger

if TYPE_CHECKING:
    from app.models.sealed import SealedProduct

logger = get_logger("integrations.pricecharting.sealed")

_PC_TCG_LABEL: dict[TcgEnum, str] = {
    TcgEnum.pokemon: "pokemon",
    TcgEnum.magic: "magic the gathering",
    TcgEnum.yugioh: "yugioh",
}

# product_type → the words PriceCharting uses in its product-name.
_PC_TYPE_LABEL: dict[SealedProductTypeEnum, str] = {
    SealedProductTypeEnum.booster_box: "booster box",
    SealedProductTypeEnum.booster_pack: "booster pack",
    SealedProductTypeEnum.etb: "elite trainer box",
    SealedProductTypeEnum.collection_box: "collection box",
    SealedProductTypeEnum.premium_collection: "premium collection",
    SealedProductTypeEnum.tin: "tin",
    SealedProductTypeEnum.blister: "blister",
    SealedProductTypeEnum.bundle: "booster bundle",
    SealedProductTypeEnum.case: "booster box case",
}

# A token that MUST appear in the matched product-name, so a fuzzy `?q=` match
# can't silently return a single card when we asked for a sealed box.
_PC_TYPE_GUARD: dict[SealedProductTypeEnum, str] = {
    SealedProductTypeEnum.booster_box: "box",
    SealedProductTypeEnum.booster_pack: "pack",
    SealedProductTypeEnum.etb: "trainer box",
    SealedProductTypeEnum.collection_box: "box",
    SealedProductTypeEnum.premium_collection: "collection",
    SealedProductTypeEnum.tin: "tin",
    SealedProductTypeEnum.blister: "blister",
    SealedProductTypeEnum.bundle: "bundle",
    SealedProductTypeEnum.case: "case",
}

_provider = PriceChartingProvider()


def _norm(s: str) -> str:
    """Lowercase + strip punctuation so set names compare across catalogs
    (``"Scarlet & Violet—Base"`` → ``"scarlet violet base"``)."""
    return " ".join("".join(c if c.isalnum() else " " for c in s).split()).lower()


async def resolve_sealed_market(product: SealedProduct) -> dict[str, Any] | None:
    """PriceCharting market price for one sealed SKU (best-effort).

    Builds a structured ``"<tcg> <set> <type>"`` query, then double-guards the
    match: the matched product-name must carry the type token *and* the matched
    console must contain the set name — otherwise PriceCharting's fuzzy ``?q=``
    drifted to an unrelated SKU (it always returns *something*). Returns
    ``{market, source, currency}`` or ``None`` (not configured, no set name, or
    no confident match) — callers fall back to other sources / MSRP.
    """
    if not _provider.is_configured() or not product.set_name:
        return None
    tcg = _PC_TCG_LABEL.get(product.tcg, "")
    typ = _PC_TYPE_LABEL.get(product.product_type, "")
    query = " ".join(part for part in (tcg, product.set_name, typ) if part).strip()
    if not query:
        return None
    try:
        mp = await _provider.get_market_price(query)
    except Exception as exc:  # never let a price lookup break a catalog read
        logger.debug("pricecharting sealed resolve failed: %s", exc)
        return None
    if mp is None or mp.market is None:
        return None
    extras = mp.extras or {}
    # Type token must be in the product-name (asked for a box, not a card).
    guard = _PC_TYPE_GUARD.get(product.product_type)
    name = str(extras.get("product_name") or "").lower()
    if guard and guard not in name:
        return None
    # Set name must be in the matched console — rejects fuzzy drift to a
    # different set (e.g. an unknown set resolving to a vintage booster box).
    if _norm(product.set_name) not in _norm(str(extras.get("console") or "")):
        return None
    return {"market": mp.market, "source": "pricecharting", "currency": "USD"}


__all__ = ["resolve_sealed_market"]
