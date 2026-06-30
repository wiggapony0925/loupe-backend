"""Environment manager for the admin developer portal (`/v1/admin/env`).

A curated, grouped catalog of the backend's environment configuration —
*what each variable is for*, *whether it's set*, and a docs link to the
relevant provider. It mirrors the security posture of ``health_service``:
secret values are **never echoed**. For a secret we surface only presence and
character count (``length``); for non-secret config (URLs, regions, model
names, the Stripe *publishable* key, …) we return the real value so an admin
can read it at a glance.

The registry below is the single source of truth. To document a new env var,
add one ``_Spec`` — the report, grouping, and masking follow automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings, get_settings
from app.schemas.ops import EnvReport, EnvVar


@dataclass(frozen=True)
class _Spec:
    """A documented binding from a Settings attribute to an env-var descriptor."""

    attr: str  # Settings attribute name
    key: str  # the ENV var name (UPPER_SNAKE)
    label: str
    group: str
    secret: bool
    description: str
    docs_url: str | None = None


# Ordered registry — groups render in this order in the UI.
_REGISTRY: tuple[_Spec, ...] = (
    # ── App ──
    _Spec(
        "app_env",
        "APP_ENV",
        "Environment",
        "App",
        False,
        "Runtime environment — development | staging | production | test.",
    ),
    _Spec(
        "app_name",
        "APP_NAME",
        "Service name",
        "App",
        False,
        "Logical service name used in logs and metrics.",
    ),
    _Spec(
        "log_level",
        "LOG_LEVEL",
        "Log level",
        "App",
        False,
        "Root logger level (DEBUG, INFO, WARNING, …).",
    ),
    _Spec(
        "log_json",
        "LOG_JSON",
        "JSON logs",
        "App",
        False,
        "Emit structured JSON logs (on in prod, off for local readability).",
    ),
    # ── Authorization ──
    _Spec(
        "admin_emails",
        "ADMIN_EMAILS",
        "Admin allowlist",
        "Authorization",
        True,
        "Comma-separated emails granted admin/portal access. Withheld (PII).",
    ),
    # ── Database & cache ──
    _Spec(
        "database_url",
        "DATABASE_URL",
        "Database URL",
        "Database & cache",
        True,
        "SQLAlchemy async DSN. Contains credentials — withheld.",
    ),
    _Spec(
        "redis_url",
        "REDIS_URL",
        "Redis URL",
        "Database & cache",
        True,
        "Queue/cache connection for the background worker. Withheld.",
    ),
    _Spec(
        "cloud_sql_connection_name",
        "CLOUD_SQL_CONNECTION_NAME",
        "Cloud SQL instance",
        "Database & cache",
        False,
        "Cloud SQL instance connection name used by the Auth Proxy / Cloud Run.",
    ),
    # ── JWT / sessions ──
    _Spec(
        "jwt_issuer",
        "JWT_ISSUER",
        "JWT issuer",
        "Auth & JWT",
        False,
        "`iss` claim minted on access/refresh tokens.",
    ),
    _Spec(
        "jwt_audience",
        "JWT_AUDIENCE",
        "JWT audience",
        "Auth & JWT",
        False,
        "`aud` claim required when verifying tokens.",
    ),
    _Spec(
        "jwt_private_key_pem",
        "JWT_PRIVATE_KEY_PEM",
        "JWT private key",
        "Auth & JWT",
        True,
        "RS256 signing key (PEM). Withheld.",
    ),
    _Spec(
        "jwt_public_key_pem",
        "JWT_PUBLIC_KEY_PEM",
        "JWT public key",
        "Auth & JWT",
        True,
        "RS256 verification key (PEM). Presence only.",
    ),
    _Spec(
        "mfa_secret_key",
        "MFA_SECRET_KEY",
        "MFA seal key",
        "Auth & JWT",
        True,
        "Fernet key that encrypts TOTP secrets at rest. Required in prod.",
    ),
    # ── Social sign-in ──
    _Spec(
        "apple_client_id",
        "APPLE_CLIENT_ID",
        "Apple client id(s)",
        "Social sign-in",
        False,
        "Accepted Apple audiences (bundle id + Services ID).",
        "https://developer.apple.com/account/resources/identifiers/list",
    ),
    _Spec(
        "google_web_client_id",
        "GOOGLE_WEB_CLIENT_ID",
        "Google web client id",
        "Social sign-in",
        False,
        "OAuth client id the web Sign-in token audience must match.",
        "https://console.cloud.google.com/apis/credentials",
    ),
    _Spec(
        "google_ios_client_id",
        "GOOGLE_IOS_CLIENT_ID",
        "Google iOS client id",
        "Social sign-in",
        False,
        "OAuth client id for native iOS Google Sign-in.",
        "https://console.cloud.google.com/apis/credentials",
    ),
    # ── Storage ──
    _Spec(
        "gcs_bucket",
        "GCS_BUCKET",
        "GCS bucket",
        "Storage",
        False,
        "Cloud Storage bucket for scan uploads (replaces S3 when set).",
    ),
    _Spec(
        "s3_bucket",
        "S3_BUCKET",
        "S3 bucket",
        "Storage",
        False,
        "Object-storage bucket for scans (S3/MinIO).",
    ),
    _Spec(
        "s3_endpoint_url",
        "S3_ENDPOINT_URL",
        "S3 endpoint",
        "Storage",
        False,
        "Custom S3 endpoint (MinIO/local); blank uses AWS.",
    ),
    _Spec(
        "s3_access_key_id",
        "S3_ACCESS_KEY_ID",
        "S3 access key id",
        "Storage",
        True,
        "Object-storage access key id. Withheld.",
    ),
    _Spec(
        "s3_secret_access_key",
        "S3_SECRET_ACCESS_KEY",
        "S3 secret key",
        "Storage",
        True,
        "Object-storage secret. Withheld.",
    ),
    _Spec(
        "reports_bucket",
        "REPORTS_BUCKET",
        "Reports bucket",
        "Storage",
        False,
        "Dedicated bucket for generated PDF statements (may contain PII).",
    ),
    # ── Catalog providers ──
    _Spec(
        "pokemon_tcg_base_url",
        "POKEMON_TCG_BASE_URL",
        "Pokémon TCG base URL",
        "Catalog providers",
        False,
        "Pokémon TCG API base.",
        "https://dev.pokemontcg.io",
    ),
    _Spec(
        "pokemon_tcg_api_key",
        "POKEMON_TCG_API_KEY",
        "Pokémon TCG key",
        "Catalog providers",
        True,
        "Optional key — raises the Pokémon TCG rate limit.",
        "https://dev.pokemontcg.io",
    ),
    _Spec(
        "scryfall_base_url",
        "SCRYFALL_BASE_URL",
        "Scryfall base URL",
        "Catalog providers",
        False,
        "Magic (Scryfall) API base — keyless.",
        "https://scryfall.com/docs/api",
    ),
    _Spec(
        "ygoprodeck_base_url",
        "YGOPRODECK_BASE_URL",
        "YGOPRODeck base URL",
        "Catalog providers",
        False,
        "Yu-Gi-Oh (YGOPRODeck) API base — keyless.",
        "https://ygoprodeck.com/api-guide/",
    ),
    _Spec(
        "apitcg_api_key",
        "APITCG_API_KEY",
        "apitcg key",
        "Catalog providers",
        True,
        "One key for One Piece / Digimon / Dragon Ball / etc. catalogs.",
        "https://www.apitcg.com",
    ),
    _Spec(
        "identify_primary_tcg",
        "IDENTIFY_PRIMARY_TCG",
        "Primary TCG",
        "Catalog providers",
        False,
        "Game the identifier biases toward on ambiguous reads ('all' disables).",
    ),
    # ── Pricing providers ──
    _Spec(
        "pricecharting_token",
        "PRICECHARTING_TOKEN",
        "PriceCharting token",
        "Pricing providers",
        True,
        "Sealed/graded price fallback source.",
        "https://www.pricecharting.com/api-documentation",
    ),
    _Spec(
        "tcgplayer_client_id",
        "TCGPLAYER_CLIENT_ID",
        "TCGplayer client id",
        "Pricing providers",
        True,
        "TCGplayer API client id.",
        "https://docs.tcgplayer.com",
    ),
    _Spec(
        "tcgplayer_client_secret",
        "TCGPLAYER_CLIENT_SECRET",
        "TCGplayer secret",
        "Pricing providers",
        True,
        "TCGplayer API secret.",
        "https://docs.tcgplayer.com",
    ),
    _Spec(
        "ebay_app_id",
        "EBAY_APP_ID",
        "eBay app id",
        "Pricing providers",
        True,
        "eBay Browse / Marketplace Insights app id.",
        "https://developer.ebay.com",
    ),
    _Spec(
        "ebay_oauth_token",
        "EBAY_OAUTH_TOKEN",
        "eBay OAuth token",
        "Pricing providers",
        True,
        "Optional pre-minted eBay OAuth token.",
        "https://developer.ebay.com",
    ),
    _Spec(
        "pokemonpricetracker_api_key",
        "POKEMONPRICETRACKER_API_KEY",
        "PokemonPriceTracker key",
        "Pricing providers",
        True,
        "Real eBay sold data for graded slabs + TCGplayer market price.",
        "https://www.pokemonpricetracker.com/api-reference",
    ),
    _Spec(
        "justtcg_api_key",
        "JUSTTCG_API_KEY",
        "JustTCG key",
        "Pricing providers",
        True,
        "Aggregated TCG prices (free tier).",
        "https://justtcg.com/api",
    ),
    _Spec(
        "psa_api_token",
        "PSA_API_TOKEN",
        "PSA token",
        "Pricing providers",
        True,
        "PSA public API token (population / cert lookups).",
        "https://www.psacard.com/publicapi",
    ),
    _Spec(
        "stockx_api_key",
        "STOCKX_API_KEY",
        "StockX API key",
        "Pricing providers",
        True,
        "StockX x-api-key (graded + sealed cards).",
        "https://developer.stockx.com",
    ),
    _Spec(
        "apify_api_token",
        "APIFY_API_TOKEN",
        "Apify token",
        "Pricing providers",
        True,
        "Facebook Marketplace nearby-listings scraper.",
        "https://apify.com",
    ),
    # ── Billing (Stripe) ──
    _Spec(
        "stripe_secret_key",
        "STRIPE_SECRET_KEY",
        "Stripe secret key",
        "Billing (Stripe)",
        True,
        "Server-side Stripe key. Withheld.",
        "https://dashboard.stripe.com/apikeys",
    ),
    _Spec(
        "stripe_publishable_key",
        "STRIPE_PUBLISHABLE_KEY",
        "Stripe publishable key",
        "Billing (Stripe)",
        False,
        "Browser-safe key for the embedded Payment Element.",
        "https://dashboard.stripe.com/apikeys",
    ),
    _Spec(
        "stripe_webhook_secret",
        "STRIPE_WEBHOOK_SECRET",
        "Stripe webhook secret",
        "Billing (Stripe)",
        True,
        "Signs incoming Stripe webhooks. Withheld.",
        "https://dashboard.stripe.com/webhooks",
    ),
    _Spec(
        "stripe_price_pro_monthly",
        "STRIPE_PRICE_PRO_MONTHLY",
        "Pro monthly price id",
        "Billing (Stripe)",
        False,
        "Stripe Price id for the monthly Pro plan.",
    ),
    _Spec(
        "stripe_price_pro_yearly",
        "STRIPE_PRICE_PRO_YEARLY",
        "Pro yearly price id",
        "Billing (Stripe)",
        False,
        "Stripe Price id for the yearly Pro plan.",
    ),
    # ── Email ──
    _Spec(
        "resend_api_key",
        "RESEND_API_KEY",
        "Resend key",
        "Email",
        True,
        "Transactional email (applicant notifications). Withheld.",
        "https://resend.com/api-keys",
    ),
    _Spec(
        "notifications_from_email",
        "NOTIFICATIONS_FROM_EMAIL",
        "From address",
        "Email",
        False,
        "Sender shown on transactional emails.",
    ),
    _Spec(
        "app_public_url",
        "APP_PUBLIC_URL",
        "Public app URL",
        "Email",
        False,
        "Base URL used to build links in emails.",
    ),
    # ── AI ──
    _Spec(
        "anthropic_api_key",
        "ANTHROPIC_API_KEY",
        "Anthropic key",
        "AI",
        True,
        "Powers the admin 'Ask your data' NL→SQL tool. Withheld.",
        "https://console.anthropic.com",
    ),
    _Spec(
        "nl_query_model",
        "NL_QUERY_MODEL",
        "NL→SQL model",
        "AI",
        False,
        "Claude model used for natural-language data queries.",
    ),
    # ── Cloud (GCP) ──
    _Spec(
        "gcp_project_id",
        "GCP_PROJECT_ID",
        "GCP project",
        "Cloud (GCP)",
        False,
        "Google Cloud project id.",
        "https://console.cloud.google.com",
    ),
    _Spec(
        "gcp_region",
        "GCP_REGION",
        "GCP region",
        "Cloud (GCP)",
        False,
        "Default Cloud Run / resource region.",
    ),
    _Spec(
        "google_application_credentials",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "Service-account path",
        "Cloud (GCP)",
        False,
        "Path to the service-account JSON the google SDKs read.",
    ),
    # ── OCR / identify ──
    _Spec(
        "ocr_provider",
        "OCR_PROVIDER",
        "OCR provider",
        "OCR & identify",
        False,
        "Card OCR backend — mock | google_vision.",
    ),
    # ── Observability ──
    _Spec(
        "sentry_dsn",
        "SENTRY_DSN",
        "Sentry DSN",
        "Observability",
        True,
        "Error/perf monitoring DSN. Presence only.",
        "https://sentry.io",
    ),
    _Spec(
        "otel_enabled",
        "OTEL_ENABLED",
        "OpenTelemetry",
        "Observability",
        False,
        "Whether OTLP tracing is wired up.",
    ),
)


def _describe(spec: _Spec, settings: Settings) -> EnvVar:
    raw = getattr(settings, spec.attr, None)

    if spec.secret:
        text = "" if raw is None else str(raw)
        return EnvVar(
            key=spec.key,
            label=spec.label,
            group=spec.group,
            secret=True,
            is_set=bool(text),
            value=None,  # never echo a secret
            length=len(text),
            description=spec.description,
            docs_url=spec.docs_url,
        )

    # Non-secret: surface the real value.
    value: str | None
    if isinstance(raw, bool):
        value, is_set = ("true" if raw else "false"), True
    elif isinstance(raw, (int, float)):
        value, is_set = str(raw), True
    else:
        text = "" if raw is None else str(raw)
        is_set = bool(text)
        value = text if is_set else None

    return EnvVar(
        key=spec.key,
        label=spec.label,
        group=spec.group,
        secret=False,
        is_set=is_set,
        value=value,
        length=len(value) if value is not None else 0,
        description=spec.description,
        docs_url=spec.docs_url,
    )


def report(settings: Settings | None = None) -> EnvReport:
    """Build the grouped, secret-safe environment report."""
    s = settings or get_settings()
    return EnvReport(
        app_env=s.app_env,
        generated_at=datetime.now(UTC),
        variables=[_describe(spec, s) for spec in _REGISTRY],
    )


__all__ = ["report"]
