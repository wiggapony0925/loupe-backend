"""Automatic PriceCharting subscription-tier detection + fallback strategy.

The app must adapt to whatever PriceCharting plan is active — Legendary,
Collector, or nothing — **with no code or config changes**. So instead of a
manual "which tier am I" flag, we *detect capabilities* by probing what the live
account can actually do, then derive the tier and the active price strategy:

    csv download works ─────────────▶ LEGENDARY  → bulk CSV mirror (best)
    api returns graded fields ──────▶ premium    → per-card API, real ladder
    api returns only the raw price ─▶ COLLECTOR   → per-card API, modeled ladder
    no / rejected token ────────────▶ NONE        → catalog + modeled ladder

The result is cached (short TTL) and re-probed on demand from the dev portal, so
the moment a subscription is upgraded or downgraded the whole app follows along
automatically. Every tier still resolves a price — we only ever *gain*
resolution as the tier improves; we never break when it drops.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.integrations.pricecharting import grades
from app.integrations.pricecharting.provider import BASE_URL, token
from app.platform.redis_client import get_redis
from app.utils.logger import get_logger

logger = get_logger("integrations.pricecharting.tiers")

_CACHE_KEY = "loupe:pricecharting:capabilities"
_CACHE_TTL = 6 * 60 * 60  # 6h — capabilities change only on plan up/downgrade.
_PROBE_TIMEOUT = 8.0
# A product that carries a full graded ladder at richer tiers, so its response
# reveals whether graded fields are exposed.
_PROBE_QUERY = "charizard base set"


class Tier(str, Enum):
    none = "none"
    collector = "collector"  # API works, raw price only
    premium = "premium"  # API returns the full graded ladder
    legendary = "legendary"  # bulk CSV download available


@dataclass(frozen=True)
class Capabilities:
    configured: bool  # a token is set
    api_ok: bool  # /api/product answered successfully
    graded_fields: bool  # the response carried real graded prices
    csv_ok: bool  # the bulk CSV download is accessible (Legendary)
    probed_at: str | None  # ISO timestamp of the probe
    note: str  # human-readable detail

    @property
    def tier(self) -> Tier:
        if self.csv_ok:
            return Tier.legendary
        if self.api_ok and self.graded_fields:
            return Tier.premium
        if self.api_ok:
            return Tier.collector
        return Tier.none

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "tier": self.tier.value}


def _unconfigured(note: str) -> Capabilities:
    return Capabilities(
        configured=bool(token()),
        api_ok=False,
        graded_fields=False,
        csv_ok=False,
        probed_at=datetime.now(UTC).isoformat(),
        note=note,
    )


async def _probe_api() -> tuple[bool, bool, str]:
    """(api_ok, graded_fields, note) from one live product lookup."""
    url = f"{BASE_URL}/product?t={token()}&q={quote(_PROBE_QUERY)}"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            resp = await client.get(url)
    except Exception as exc:
        return False, False, f"API probe failed: {exc}"
    if resp.status_code == 401 or resp.status_code == 403:
        return False, False, "Token rejected (401/403) — check the subscription."
    if resp.status_code >= 400:
        return False, False, f"API probe HTTP {resp.status_code}."
    data = resp.json() or {}
    if data.get("status") != "success":
        return False, False, f"API status={data.get('status')!r}."
    graded = grades.has_graded_fields(data)
    return True, graded, "Graded fields present." if graded else "Raw price only."


async def _probe_csv() -> tuple[bool, str]:
    """(csv_ok, note) — the CSV bulk download is Legendary-only. Its URL is
    account-specific (from the Subscriptions page), so it's config, not a guess;
    when unset we simply stay on the API path."""
    csv_url = get_settings().pricecharting_csv_url
    if not csv_url:
        return False, "CSV URL not set (PRICECHARTING_CSV_URL) — API path in use."
    try:
        async with httpx.AsyncClient(
            timeout=_PROBE_TIMEOUT, follow_redirects=True
        ) as c:
            # Range header keeps the probe to the first bytes, not the whole file.
            resp = await c.get(csv_url, headers={"Range": "bytes=0-2048"})
    except Exception as exc:
        return False, f"CSV probe failed: {exc}"
    if resp.status_code in (401, 403):
        return False, "CSV download forbidden — not a Legendary subscription."
    if resp.status_code >= 400:
        return False, f"CSV probe HTTP {resp.status_code}."
    return True, "CSV bulk download available."


async def detect(*, force: bool = False) -> Capabilities:
    """Current capabilities — cached, re-probed on ``force`` (dev-portal button)."""
    if not token():
        return _unconfigured("No PriceCharting token configured.")
    if not force:
        cached = await _cache_get()
        if cached is not None:
            return cached
    api_ok, graded, api_note = await _probe_api()
    csv_ok, csv_note = await _probe_csv()
    caps = Capabilities(
        configured=True,
        api_ok=api_ok,
        graded_fields=graded,
        csv_ok=csv_ok,
        probed_at=datetime.now(UTC).isoformat(),
        note=f"{api_note} {csv_note}".strip(),
    )
    await _cache_set(caps)
    return caps


async def _cache_get() -> Capabilities | None:
    try:
        raw = await (await get_redis()).get(_CACHE_KEY)
    except Exception:  # pragma: no cover - cache is best-effort
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        data.pop("tier", None)  # derived, not a constructor field
        return Capabilities(**data)
    except (TypeError, ValueError):
        return None


async def _cache_set(caps: Capabilities) -> None:
    try:
        r = await get_redis()
        await r.setex(_CACHE_KEY, _CACHE_TTL, json.dumps(asdict(caps)))
    except Exception:  # pragma: no cover - cache is best-effort
        pass


# ── Strategy descriptions (what each tier does; drives the dev-portal page) ──


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    description: str


_STRATEGY: dict[Tier, Strategy] = {
    Tier.legendary: Strategy(
        "bulk_mirror",
        "Bulk CSV mirror",
        "The entire PriceCharting price guide is mirrored locally and refreshed "
        "daily — instant, unlimited, quota-free raw + graded prices for every "
        "card. The per-card API stays as the live fallback.",
    ),
    Tier.premium: Strategy(
        "api_graded",
        "Per-card API — real grade ladder",
        "Live per-card lookups return the full real grade ladder "
        "(PSA / BGS / CGC / SGC). Throttled to 1 request/second.",
    ),
    Tier.collector: Strategy(
        "api_raw",
        "Per-card API — raw price",
        "Live per-card lookups return the raw price; the graded ladder is "
        "modeled from it. Throttled to 1 request/second.",
    ),
    Tier.none: Strategy(
        "modeled",
        "No PriceCharting",
        "No usable token — prices come from the card catalog and other "
        "providers, and the graded ladder is fully modeled.",
    ),
}

_TIER_LABEL: dict[Tier, str] = {
    Tier.legendary: "Legendary",
    Tier.premium: "Premium (API + graded)",
    Tier.collector: "Collector (API, raw)",
    Tier.none: "None / Free",
}

_TIER_REQUIREMENT: dict[Tier, str] = {
    Tier.legendary: "CSV bulk download accessible",
    Tier.premium: "API returns graded price fields",
    Tier.collector: "API returns the raw price",
    Tier.none: "No token (or token rejected)",
}

#: Best → worst, for the fallback-chain visualisation.
_TIER_ORDER: tuple[Tier, ...] = (
    Tier.legendary,
    Tier.premium,
    Tier.collector,
    Tier.none,
)


def strategy_for(tier: Tier) -> Strategy:
    return _STRATEGY[tier]


def describe(
    caps: Capabilities, mirror: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The full picture the dev-portal page renders: current tier, capabilities,
    the active strategy, the whole fallback chain (which rung is active), the
    grade-field mapping, and the local mirror status."""
    active = caps.tier
    active_strategy = _STRATEGY[active]
    chain = [
        {
            "tier": t.value,
            "label": _TIER_LABEL[t],
            "requirement": _TIER_REQUIREMENT[t],
            "strategy": asdict(_STRATEGY[t]),
            "active": t == active,
        }
        for t in _TIER_ORDER
    ]
    return {
        "configured": caps.configured,
        "capabilities": caps.to_dict(),
        "tier": {"key": active.value, "label": _TIER_LABEL[active]},
        "strategy": asdict(active_strategy),
        "fallback_chain": chain,
        "grade_map": [
            {"field": field, "grade": grade}
            for field, grade in grades.CARD_GRADE_LABELS
        ],
        "mirror": mirror or {"ready": False, "rows": 0, "synced_at": None},
    }


__all__ = [
    "Capabilities",
    "Strategy",
    "Tier",
    "describe",
    "detect",
    "strategy_for",
]
