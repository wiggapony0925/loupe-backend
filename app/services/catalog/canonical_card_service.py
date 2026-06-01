"""Canonical Card composer.

Fans out across every catalog + provider for a given card id and builds
one :class:`CanonicalCard` document. The endpoint
``GET /v1/cards/{id}/canonical`` returns exactly this shape.

Composition rules (intentionally simple):

* Identity / set / images / attributes come from the live catalog
  (Pokémon TCG → Scryfall → YGOPRODeck → local DB fallback).
* Pricing quotes come from every provider that exposes
  ``get_market_price``; the consensus is the median of the quotes'
  ``market`` values.
* Population rows come from every provider that exposes
  ``get_population`` (today: none on the free tier; reserved for paid
  PSA / CGC / GoCollect integrations).
* Listings come from ``fan_out_listings`` (eBay).
* Comps come from ``fan_out_comps`` (130point, eBay).
* Certs are filled only when the card id is a ``psa:<cert>`` lookup.
* Graded rows are taken from the existing market service when present,
  marked ``source = "real" | "synthesized"`` according to its own
  bookkeeping.

Each section's contributing providers are listed in ``provenance`` so the
frontend can render real-vs-synthesized badges. All upstream errors are
swallowed and recorded in ``provenance.errors``.
"""

from __future__ import annotations

import asyncio
import statistics
from typing import Any

from app.integrations import get_registry
from app.schemas.canonical_card import (
    CANONICAL_CARD_VERSION,
    CanonicalAttributes,
    CanonicalCard,
    CanonicalCert,
    CanonicalComp,
    CanonicalIdentity,
    CanonicalListing,
    CanonicalPopulation,
    CanonicalPricing,
    CanonicalProvenance,
    CanonicalSet,
    GradedPriceRow,
    ImageAsset,
    ImageSet,
    Money,
    PopulationRow,
    PriceQuote,
)
from app.services.catalog import card_search_service
from app.services.market import market_service
from app.utils.logger import get_logger
from app.utils.time import utcnow

logger = get_logger("services.canonical_card")

_FANOUT_TIMEOUT_S = 5.0


# --------------------------------------------------------------------- query


def _query_from_card(card: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("name", "set_name", "number"):
        val = card.get(key)
        if val:
            parts.append(str(val) if key != "number" else f"#{val}")
    return " ".join(parts).strip()


# --------------------------------------------------------------------- mappers


def _money_from_dict(d: Any) -> Money | None:
    if not isinstance(d, dict):
        return None
    amt = d.get("amount")
    if amt is None:
        return None
    try:
        return Money(amount=float(amt), currency=str(d.get("currency") or "USD"))
    except (TypeError, ValueError):
        return None


def _image_asset(d: Any) -> ImageAsset | None:
    if not isinstance(d, dict):
        return None
    url = d.get("url")
    if not url:
        return None
    return ImageAsset(
        url=str(url),
        width=d.get("width") if isinstance(d.get("width"), int) else None,
        height=d.get("height") if isinstance(d.get("height"), int) else None,
        alt=d.get("alt") if isinstance(d.get("alt"), str) else None,
    )


def _image_set_from_card(card: dict[str, Any]) -> ImageSet | None:
    raw = card.get("images")
    if isinstance(raw, dict):
        images = ImageSet(
            small=_image_asset(raw.get("small")),
            normal=_image_asset(raw.get("normal")),
            large=_image_asset(raw.get("large")),
            art_crop=_image_asset(raw.get("art_crop")),
        )
        if any([images.small, images.normal, images.large, images.art_crop]):
            return images
    url = card.get("image_url")
    if url:
        asset = ImageAsset(url=str(url))
        return ImageSet(small=asset, normal=asset, large=asset)
    return None


def _set_from_card(card: dict[str, Any]) -> CanonicalSet | None:
    set_block = card.get("set")
    if isinstance(set_block, dict):
        return CanonicalSet(
            id=set_block.get("id"),
            code=set_block.get("code"),
            name=set_block.get("name"),
            series=set_block.get("series"),
            release_date=set_block.get("release_date"),
            printed_total=set_block.get("printed_total"),
            total_cards=set_block.get("total_cards"),
            logo=_image_asset(set_block.get("logo")),
            symbol=_image_asset(set_block.get("symbol")),
        )
    if card.get("set_name") or card.get("set_code"):
        return CanonicalSet(name=card.get("set_name"), code=card.get("set_code"))
    return None


def _attributes_from_card(card: dict[str, Any]) -> CanonicalAttributes | None:
    attrs = card.get("attributes")
    if not isinstance(attrs, dict) or not attrs:
        return None
    try:
        return CanonicalAttributes(**attrs)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("canonical attributes coerce failed: %s", exc)
        return None


def _pricing_summary_to_quote(card: dict[str, Any]) -> PriceQuote | None:
    summary = card.get("pricing_summary")
    if not isinstance(summary, dict):
        return None
    sources = summary.get("sources")
    source = (
        sources[0]
        if isinstance(sources, list) and sources
        else card.get("source") or "catalog"
    )
    market = _money_from_dict(summary.get("market"))
    low = _money_from_dict(summary.get("low"))
    mid = _money_from_dict(summary.get("mid"))
    high = _money_from_dict(summary.get("high"))
    if not any([market, low, mid, high]):
        return None
    return PriceQuote(
        source=str(source),
        market=market,
        low=low,
        mid=mid,
        high=high,
        as_of=summary.get("as_of"),
        sample_size=summary.get("sample_size"),
    )


def _market_price_to_quote(mp: Any) -> PriceQuote | None:
    if mp is None:
        return None
    currency = getattr(mp, "currency", "USD") or "USD"

    def _m(val: Any) -> Money | None:
        if val is None:
            return None
        try:
            return Money(amount=float(val), currency=currency)
        except (TypeError, ValueError):
            return None

    market = _m(getattr(mp, "market", None))
    low = _m(getattr(mp, "low", None))
    mid = _m(getattr(mp, "mid", None))
    high = _m(getattr(mp, "high", None))
    if not any([market, low, mid, high]):
        return None
    extras = getattr(mp, "extras", None) or {}
    return PriceQuote(
        source=str(getattr(mp, "source", "unknown")),
        market=market,
        low=low,
        mid=mid,
        high=high,
        as_of=extras.get("as_of") if isinstance(extras, dict) else None,
        sample_size=extras.get("sample_size") if isinstance(extras, dict) else None,
        url=extras.get("url") if isinstance(extras, dict) else None,
    )


def _consensus(quotes: list[PriceQuote], currency: str) -> Money | None:
    amounts = [q.market.amount for q in quotes if q.market is not None]
    if not amounts:
        return None
    return Money(amount=float(statistics.median(amounts)), currency=currency)


# --------------------------------------------------------------------- graded


def _graded_rows_from_market(snapshot: dict[str, Any] | None) -> list[GradedPriceRow]:
    if not isinstance(snapshot, dict):
        return []
    houses = snapshot.get("houses")
    if not isinstance(houses, list):
        return []
    rows: list[GradedPriceRow] = []
    for block in houses:
        if not isinstance(block, dict):
            continue
        house = block.get("house")
        for r in block.get("grades") or []:
            if not isinstance(r, dict):
                continue
            money = _money_from_dict(r.get("market"))
            if money is None:
                continue
            try:
                grade_raw = r.get("grade")
                if grade_raw is None:
                    continue
                grade_val = float(grade_raw)
            except (TypeError, ValueError):
                continue
            rows.append(
                GradedPriceRow(
                    house=str(r.get("house") or house or "unknown"),
                    grade=grade_val,
                    grade_label=str(r.get("grade_label") or r.get("grade") or ""),
                    market=money,
                    population=r.get("population"),
                    change_pct=r.get("change_pct"),
                    last_sale_at=r.get("last_sale_at"),
                    listing_url=r.get("listing_url"),
                    source=("real" if r.get("source") == "real" else "synthesized"),
                )
            )
    return rows


def _summary_from_market(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    summary = snapshot.get("summary")
    if not isinstance(summary, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("pop_total", "change_pct_1y"):
        if key in summary:
            out[key] = summary[key]
    for key in ("raw", "graded_avg", "pop_top"):
        money = _money_from_dict(summary.get(key))
        if money is not None:
            out[key] = money.amount
    if summary.get("last_sale_at"):
        out["last_sale_at"] = summary["last_sale_at"]
    return out


# --------------------------------------------------------------------- compose


async def _safe(coro: Any, *, label: str, errors: list[str]) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout=_FANOUT_TIMEOUT_S)
    except TimeoutError:
        # Bare TimeoutError has no message — logging `%s` produced empty
        # strings ("canonical compose market failed: ") that made these
        # very common failures impossible to spot in production logs.
        logger.info(
            "canonical compose %s failed: timeout after %ss", label, _FANOUT_TIMEOUT_S
        )
        errors.append(f"{label}:Timeout")
        return None
    except Exception as exc:
        logger.info("canonical compose %s failed: %r", label, exc)
        errors.append(f"{label}:{type(exc).__name__}")
        return None


async def compose_canonical_card(card_id: str) -> CanonicalCard | None:
    """Build the full :class:`CanonicalCard` for a card id.

    Returns ``None`` if the catalog can't find the card. Any provider
    failure is recorded in ``provenance.errors`` but never raises.
    """
    errors: list[str] = []

    base = await _safe(
        card_search_service.get_card(card_id), label="catalog", errors=errors
    )
    if not isinstance(base, dict) or not base.get("id"):
        return None

    query = _query_from_card(base)
    registry = get_registry()

    market_task = _safe(
        market_service.get_card_market(card_id), label="market", errors=errors
    )
    listings_task = _safe(
        registry.fan_out_listings(query, limit=20), label="listings", errors=errors
    )
    comps_task = _safe(
        registry.fan_out_comps(query, days=90, limit=50), label="comps", errors=errors
    )
    quotes_task = _safe(
        registry.fan_out_market_price(query), label="quotes", errors=errors
    )
    population_task = _safe(
        registry.fan_out_population(query), label="population", errors=errors
    )
    (
        market_snapshot,
        raw_listings,
        raw_comps,
        raw_quotes,
        raw_pops,
    ) = await asyncio.gather(
        market_task, listings_task, comps_task, quotes_task, population_task
    )

    # ---- pricing ------------------------------------------------------
    quotes: list[PriceQuote] = []
    catalog_quote = _pricing_summary_to_quote(base)
    if catalog_quote is not None:
        quotes.append(catalog_quote)
    if isinstance(raw_quotes, list):
        for mp in raw_quotes:
            q = _market_price_to_quote(mp)
            if q is not None:
                quotes.append(q)

    snapshot = (
        market_snapshot.get("snapshot") if isinstance(market_snapshot, dict) else None
    )
    graded_rows = _graded_rows_from_market(snapshot)
    pricing = CanonicalPricing(
        consensus=_consensus(quotes, "USD"),
        currency="USD",
        quotes=quotes,
        graded=graded_rows,
        summary=_summary_from_market(snapshot),
    )

    # ---- population --------------------------------------------------
    pop_rows: list[PopulationRow] = []
    by_house: dict[str, int] = {}
    pop_total = 0
    if isinstance(raw_pops, list):
        for p in raw_pops:
            try:
                row = PopulationRow(
                    source=str(getattr(p, "source", "unknown")),
                    house=str(getattr(p, "house", "unknown")),
                    grade=str(getattr(p, "grade", "")),
                    population=int(getattr(p, "population", 0) or 0),
                    pop_higher=getattr(p, "pop_higher", None),
                )
            except Exception:
                continue
            pop_rows.append(row)
            by_house[row.house] = by_house.get(row.house, 0) + row.population
            pop_total += row.population
    population = CanonicalPopulation(rows=pop_rows, total=pop_total, by_house=by_house)

    # ---- listings ----------------------------------------------------
    canonical_listings: list[CanonicalListing] = []
    if isinstance(raw_listings, list):
        for x in raw_listings:
            try:
                canonical_listings.append(
                    CanonicalListing(
                        source=str(x.source),
                        title=str(x.title),
                        price=Money(amount=float(x.price), currency=x.currency),
                        url=str(x.url or ""),
                        condition=x.condition,
                        image_url=x.image_url,
                        is_auction=bool(x.is_auction),
                        time_left_seconds=x.time_left_seconds,
                    )
                )
            except Exception:
                continue

    # ---- comps -------------------------------------------------------
    canonical_comps: list[CanonicalComp] = []
    if isinstance(raw_comps, list):
        for c in raw_comps:
            try:
                canonical_comps.append(
                    CanonicalComp(
                        source=str(c.source),
                        title=str(c.title),
                        price=Money(amount=float(c.price), currency=c.currency),
                        sold_at=str(c.sold_at),
                        condition=c.condition,
                        grade=c.grade,
                        house=c.house,
                        url=c.url,
                        image_url=c.image_url,
                    )
                )
            except Exception:
                continue

    # ---- cert (PSA-style id) ----------------------------------------
    certs: list[CanonicalCert] = []
    if card_id.lower().startswith("psa:"):
        cert_no = card_id.split(":", 1)[1]
        psa = registry.get("psa")
        verify = getattr(psa, "verify_cert", None) if psa else None
        if verify is not None and getattr(psa, "is_configured", lambda: False)():
            verified = await _safe(verify(cert_no), label="psa_cert", errors=errors)
            if isinstance(verified, dict):
                certs.append(
                    CanonicalCert(
                        house="psa",
                        cert_number=str(verified.get("CertNumber") or cert_no),
                        grade=str(
                            verified.get("CardGrade") or verified.get("Grade") or ""
                        )
                        or None,
                        subject=verified.get("Subject"),
                        year=verified.get("Year"),
                        brand=verified.get("Brand"),
                        category=verified.get("Category"),
                        verified_at=utcnow().isoformat(),
                    )
                )

    # ---- identity / set / images / attrs ----------------------------
    identity = CanonicalIdentity(
        id=str(base["id"]),
        name=str(base.get("name") or ""),
        tcg=str(base.get("tcg") or "unknown"),
        number=base.get("number"),
        rarity=base.get("rarity"),
        year=base.get("year"),
        language="en",
        tags=list(base.get("tags") or []),
    )

    # ---- provenance -------------------------------------------------
    catalog_source = base.get("source") or "catalog"
    provenance = CanonicalProvenance(
        identity_source=str(catalog_source),
        set_source=str(catalog_source),
        image_source=str(catalog_source),
        pricing_sources=sorted({q.source for q in quotes}),
        population_sources=sorted({r.source for r in pop_rows}),
        listings_sources=sorted({listing.source for listing in canonical_listings}),
        comps_sources=sorted({c.source for c in canonical_comps}),
        cert_sources=sorted({c.house for c in certs}),
        composed_at=utcnow().isoformat(),
        errors=errors,
    )

    return CanonicalCard(
        schema_version=CANONICAL_CARD_VERSION,
        identity=identity,
        set=_set_from_card(base),
        images=_image_set_from_card(base),
        attributes=_attributes_from_card(base),
        pricing=pricing,
        population=population,
        listings=canonical_listings,
        comps=canonical_comps,
        certs=certs,
        provenance=provenance,
    )


__all__ = ["compose_canonical_card"]
