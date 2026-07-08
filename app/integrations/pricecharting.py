"""PriceCharting provider — graded + raw market prices.

API docs: https://www.pricecharting.com/api-documentation
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from app.config import get_settings
from app.integrations.base import BaseProvider, MarketPrice
from app.models.enums import SealedProductTypeEnum, TcgEnum
from app.utils.logger import get_logger

if TYPE_CHECKING:
    from app.models.sealed import SealedProduct

logger = get_logger("integrations.pricecharting")

_BASE = "https://www.pricecharting.com/api"


def _token() -> str | None:
    s = get_settings()
    return s.pricecharting_token or s.pricecharting_api_key or None


# PriceCharting reuses its video-game price columns for trading cards; each
# column maps to a specific graded market (see the Prices API "Description of
# Keys"). We pull the WHOLE ladder out of the single call we already make so
# card-detail can show real per-grade prices instead of a modeled estimate.
# Ordered low → high grade; the "10" tiers are house-specific in the API.
_CARD_GRADE_LABELS: tuple[tuple[str, str], ...] = (
    ("loose-price", "UNGRADED"),  # raw / ungraded
    ("cib-price", "PSA 7"),  # "Grade 7 or 7.5"
    ("new-price", "PSA 8"),  # "Grade 8 or 8.5"
    ("graded-price", "PSA 9"),  # "Grade 9"
    ("box-only-price", "BGS 9.5"),  # "Grade 9.5" (PSA doesn't issue 9.5)
    ("manual-only-price", "PSA 10"),  # explicitly "Graded 10 by PSA"
    ("bgs-10-price", "BGS 10"),
    ("condition-17-price", "CGC 10"),
    ("condition-18-price", "SGC 10"),
)


def _cents_to_dollars(value: Any) -> float | None:
    """PriceCharting encodes every price as an integer number of pennies."""
    try:
        return round(int(value) / 100.0, 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _card_grade_ladder(data: dict[str, Any]) -> dict[str, float]:
    """The full per-grade price ladder present in a product response
    (``grade label → USD``). Absent / zero grades are omitted, so a token whose
    tier only returns the raw price yields ``{"UNGRADED": …}`` and richer tiers
    light up the rest automatically."""
    ladder: dict[str, float] = {}
    for key, label in _CARD_GRADE_LABELS:
        price = _cents_to_dollars(data.get(key))
        if price is not None and price > 0:
            ladder[label] = price
    return ladder


class PriceChartingProvider(BaseProvider):
    id = "pricecharting"
    name = "PriceCharting"

    def is_configured(self) -> bool:
        return _token() is not None

    async def get_market_price(self, query: str) -> MarketPrice | None:
        if not self.is_configured() or not query:
            return None
        tok = _token()
        url = f"{_BASE}/product?t={tok}&q={quote(query)}"
        try:
            resp = await self._call_with_retry("GET", url)
            if resp is None or resp.status_code >= 400:
                return None
            data = resp.json() or {}
            return self._reduce(data)
        except Exception as exc:
            logger.warning("pricecharting get_market_price failed: %s", exc)
            return None

    @staticmethod
    def _reduce(data: dict[str, Any]) -> MarketPrice | None:
        loose = _cents_to_dollars(data.get("loose-price"))
        new = _cents_to_dollars(data.get("new-price"))
        graded = _cents_to_dollars(data.get("graded-price")) or _cents_to_dollars(
            data.get("manual-only-price")
        )
        ladder = _card_grade_ladder(data)
        if not any((loose, new, graded)) and not ladder:
            return None
        # Keep the low/mid/high/market shape stable (valuation depends on it) and
        # carry the *rest* of the response — the full grade ladder, yearly sales
        # volume, PriceCharting id, release date — in extras so downstream can
        # surface it without a second API call.
        extras: dict[str, Any] = {
            "product_name": data.get("product-name"),
            "console": data.get("console-name"),
        }
        if ladder:
            extras["grade_ladder"] = ladder
        sales_volume = _int_or_none(data.get("sales-volume"))
        if sales_volume is not None:
            extras["sales_volume"] = sales_volume
        if data.get("id"):
            extras["pc_id"] = str(data.get("id"))
        if data.get("release-date"):
            extras["release_date"] = data.get("release-date")
        return MarketPrice(
            source="pricecharting",
            market=graded or loose or new,
            low=loose,
            mid=new,
            high=graded,
            extras=extras,
        )


# ── Sealed-product market resolution ──────────────────────────────────────
#
# PriceCharting indexes sealed product unusually cleanly: the console is
# "Pokemon <Set>" and the product-name is just the type ("Booster Box",
# "Elite Trainer Box", ...). A structured `?q=` query therefore resolves the
# exact SKU — a *more* reliable sealed price source than fuzzy-matching the
# verbose TCGplayer catalog names. We use it to fill the gaps TCGplayer misses.

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


__all__ = ["PriceChartingProvider", "resolve_sealed_market"]
