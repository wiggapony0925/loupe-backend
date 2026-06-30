"""Second-party / external-service catalog for the admin developer portal
(`/v1/admin/integrations`).

Surfaces every *other company's* API the app depends on — TCG catalogs,
pricing/market sources, payments, email, AI, monitoring, infra — with what we
use it for, whether it's configured, the capabilities it serves, a docs link,
and (on demand) a **live reachability probe**.

The configured/capability truth comes from the real
:class:`~app.integrations.registry.ProviderRegistry` (so it can't drift from
what actually runs); this module enriches each provider with presentation
metadata and folds in the platform services that live outside that registry
(Stripe, Resend, Anthropic, Sentry, GCP) plus the keyless catalog APIs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings, get_settings
from app.integrations import get_registry
from app.integrations.base import get_http_client
from app.schemas.ops import Integration, IntegrationsReport
from app.utils.logger import get_logger

logger = get_logger("admin.integrations")


@dataclass(frozen=True)
class _Meta:
    """Presentation metadata for one external service."""

    category: str
    purpose: str
    docs_url: str | None = None
    # Public URL pinged by the live probe. Any HTTP response counts as
    # "reachable" (even 401/404) — only a transport failure means "down".
    # None ⇒ never probed (e.g. Sentry/GCP, where reachability isn't meaningful).
    probe_url: str | None = None


# Metadata for the providers that live in the ProviderRegistry, keyed by id.
_PROVIDER_META: dict[str, _Meta] = {
    "pokemon_tcg": _Meta(
        "Catalog",
        "Pokémon card catalog + images.",
        "https://dev.pokemontcg.io",
        "https://api.pokemontcg.io/v2/sets?pageSize=1",
    ),
    "tcgdex": _Meta(
        "Catalog",
        "Multilingual TCG catalog fallback.",
        "https://tcgdex.dev",
        "https://api.tcgdex.net/v2/en/series",
    ),
    "tcgplayer": _Meta(
        "Pricing & market",
        "TCGplayer market prices.",
        "https://docs.tcgplayer.com",
        "https://api.tcgplayer.com",
    ),
    "tcgcsv": _Meta(
        "Pricing & market",
        "Free daily TCGplayer price mirror.",
        "https://tcgcsv.com",
        "https://tcgcsv.com",
    ),
    "pricecharting": _Meta(
        "Pricing & market",
        "Sealed + graded price fallback.",
        "https://www.pricecharting.com/api-documentation",
        "https://www.pricecharting.com",
    ),
    "pokemonpricetracker": _Meta(
        "Pricing & market",
        "eBay sold data for graded slabs + TCGplayer price.",
        "https://www.pokemonpricetracker.com/api-reference",
        "https://www.pokemonpricetracker.com",
    ),
    "justtcg": _Meta(
        "Pricing & market",
        "Aggregated TCG prices (One Piece/Digimon/Lorcana).",
        "https://justtcg.com/api",
        "https://api.justtcg.com",
    ),
    "ebay": _Meta(
        "Pricing & market",
        "Live listings + Marketplace Insights sold comps.",
        "https://developer.ebay.com",
        "https://api.ebay.com",
    ),
    "130point": _Meta(
        "Pricing & market",
        "Recent eBay/auction sold comps.",
        "https://130point.com",
        "https://130point.com",
    ),
    "psa": _Meta(
        "Pricing & market",
        "PSA population + cert verification.",
        "https://www.psacard.com/publicapi",
        "https://api.psacard.com",
    ),
    "stockx": _Meta(
        "Pricing & market",
        "Graded + sealed card market data.",
        "https://developer.stockx.com",
        "https://api.stockx.com",
    ),
    "gocollect": _Meta(
        "Pricing & market",
        "Graded-card values (stub).",
        "https://gocollect.com",
        "https://gocollect.com",
    ),
    "apify_fb": _Meta(
        "Pricing & market",
        "Facebook Marketplace nearby-listings scraper.",
        "https://apify.com",
        "https://api.apify.com",
    ),
}


@dataclass(frozen=True)
class _Extra:
    """A platform/catalog service that isn't in the market ProviderRegistry."""

    id: str
    name: str
    meta: _Meta

    def configured(self, s: Settings) -> bool:
        return _EXTRA_CONFIGURED[self.id](s)


_EXTRA_CONFIGURED: dict[str, Callable[[Settings], bool]] = {
    "scryfall": lambda s: True,  # keyless public API
    "ygoprodeck": lambda s: True,  # keyless public API
    "apitcg": lambda s: bool(s.apitcg_api_key),
    "stripe": lambda s: bool(s.stripe_secret_key),
    "resend": lambda s: s.email_enabled,
    "anthropic": lambda s: bool(s.anthropic_api_key),
    "sentry": lambda s: bool(s.sentry_dsn),
    "gcp": lambda s: bool(s.gcp_project_id or s.cloud_sql_connection_name),
}

_EXTRAS: tuple[_Extra, ...] = (
    _Extra(
        "scryfall",
        "Scryfall",
        _Meta(
            "Catalog",
            "Magic: The Gathering catalog + prices (keyless).",
            "https://scryfall.com/docs/api",
            "https://api.scryfall.com/sets",
        ),
    ),
    _Extra(
        "ygoprodeck",
        "YGOPRODeck",
        _Meta(
            "Catalog",
            "Yu-Gi-Oh! catalog (keyless).",
            "https://ygoprodeck.com/api-guide/",
            "https://db.ygoprodeck.com",
        ),
    ),
    _Extra(
        "apitcg",
        "apitcg",
        _Meta(
            "Catalog",
            "One Piece / Digimon / Dragon Ball / etc. catalogs.",
            "https://www.apitcg.com",
            "https://www.apitcg.com",
        ),
    ),
    _Extra(
        "stripe",
        "Stripe",
        _Meta(
            "Payments",
            "Loupe Pro subscriptions (checkout + webhooks).",
            "https://dashboard.stripe.com",
            "https://api.stripe.com",
        ),
    ),
    _Extra(
        "resend",
        "Resend",
        _Meta(
            "Email",
            "Transactional email (applicant notifications).",
            "https://resend.com",
            "https://api.resend.com",
        ),
    ),
    _Extra(
        "anthropic",
        "Anthropic (Claude)",
        _Meta(
            "AI",
            "Powers the admin 'Ask your data' NL→SQL tool.",
            "https://console.anthropic.com",
            "https://api.anthropic.com",
        ),
    ),
    _Extra(
        "sentry",
        "Sentry",
        _Meta(
            "Monitoring", "Error + performance monitoring.", "https://sentry.io", None
        ),
    ),
    _Extra(
        "gcp",
        "Google Cloud",
        _Meta(
            "Infrastructure",
            "Cloud Run, Cloud SQL, Storage, Secret Manager.",
            "https://console.cloud.google.com",
            None,
        ),
    ),
)

# Category display order.
_CATEGORY_ORDER = (
    "Catalog",
    "Pricing & market",
    "Payments",
    "Email",
    "AI",
    "Monitoring",
    "Infrastructure",
)

_PROBE_TIMEOUT_S = 4.0


def _collect(settings: Settings) -> list[Integration]:
    """Build the (un-probed) integration list from the registry + extras."""
    items: list[Integration] = []

    # Registry-backed market/catalog providers (authoritative configured state).
    for row in get_registry().status():
        meta = _PROVIDER_META.get(row["id"])
        if meta is None:
            # Unknown provider — still surface it, uncategorised.
            meta = _Meta("Pricing & market", "External data provider.")
        items.append(
            Integration(
                id=row["id"],
                name=row["name"],
                category=meta.category,
                purpose=meta.purpose,
                configured=bool(row["configured"]),
                capabilities=list(row.get("capabilities", [])),
                docs_url=meta.docs_url,
                status="ready" if row["configured"] else "unconfigured",
            )
        )

    # Platform / keyless-catalog services outside the market registry.
    for extra in _EXTRAS:
        configured = extra.configured(settings)
        items.append(
            Integration(
                id=extra.id,
                name=extra.name,
                category=extra.meta.category,
                purpose=extra.meta.purpose,
                configured=configured,
                capabilities=[],
                docs_url=extra.meta.docs_url,
                status="ready" if configured else "unconfigured",
            )
        )

    items.sort(key=lambda i: (_category_rank(i.category), i.name.lower()))
    return items


def _category_rank(category: str) -> int:
    try:
        return _CATEGORY_ORDER.index(category)
    except ValueError:
        return len(_CATEGORY_ORDER)


def _probe_url_for(integration_id: str) -> str | None:
    meta = _PROVIDER_META.get(integration_id)
    if meta:
        return meta.probe_url
    for extra in _EXTRAS:
        if extra.id == integration_id:
            return extra.meta.probe_url
    return None


async def _probe(item: Integration) -> None:
    """Ping a configured service's public URL; mutate status/latency in place."""
    url = _probe_url_for(item.id)
    if url is None:
        return  # not meaningfully probe-able — leave as "ready"
    client = await get_http_client()
    started = time.perf_counter()
    try:
        resp = await client.get(url, timeout=_PROBE_TIMEOUT_S)
        item.latency_ms = int((time.perf_counter() - started) * 1000)
        item.http_status = resp.status_code
        # Any response means we reached the service (auth/404 still = reachable).
        item.status = "live"
        item.detail = f"Reachable · HTTP {resp.status_code}"
    except Exception as exc:
        item.latency_ms = int((time.perf_counter() - started) * 1000)
        item.status = "down"
        item.detail = f"Unreachable ({type(exc).__name__})"


async def report(
    probe: bool = False, settings: Settings | None = None
) -> IntegrationsReport:
    """Build the integrations report. When ``probe`` is true, configured services
    with a public URL are pinged concurrently for live reachability."""
    s = settings or get_settings()
    items = _collect(s)

    if probe:
        await asyncio.gather(
            *(_probe(i) for i in items if i.configured), return_exceptions=True
        )

    return IntegrationsReport(
        generated_at=datetime.now(UTC), probed=probe, integrations=items
    )


__all__ = ["report"]
