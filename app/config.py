"""Application configuration via Pydantic settings + .env loading."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class Settings(BaseSettings):
    """Strongly-typed runtime configuration for loupe-backend.

    Values are sourced from environment variables (and optionally a ``.env``
    file in the project root), with Google Cloud Secret Manager filling any
    unset secrets in production. Access the singleton via :func:`get_settings`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Add Secret Manager *below* env so explicit env vars still win, and it
        only fills the gaps. The source is a hard-gated no-op outside production
        (see app.platform.secret_manager)."""
        from app.platform.secret_manager import GoogleSecretManagerSource

        return (
            init_settings,
            env_settings,
            GoogleSecretManagerSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    # --- App ---
    app_env: Literal["development", "staging", "production", "test"] = "development"
    app_name: str = "loupe-backend"
    log_level: str = "INFO"
    log_json: bool = False

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./loupe.db"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- JWT ---
    jwt_issuer: str = "https://api.loupe.app"
    jwt_audience: str = "loupe-mobile"
    jwt_private_key_pem: str = ""
    jwt_public_key_pem: str = ""
    jwt_access_ttl_seconds: int = 900
    jwt_refresh_ttl_seconds: int = 2_592_000
    jwt_algorithm: str = "RS256"
    # Seconds of clock-skew tolerance applied to ``iat``/``exp``/``nbf`` so
    # tiny drift between pods doesn't manifest as 401s for valid users.
    jwt_leeway_seconds: int = 30
    # Short-lived token issued after the password step when MFA is on; the
    # client exchanges it (+ a TOTP/backup code) for a real token pair.
    jwt_mfa_ttl_seconds: int = 300

    # --- Rate limiting ---
    # Number of *trusted* reverse proxies in front of the app, used to pick the
    # real client IP out of ``X-Forwarded-For``. A client can spoof the LEFT end
    # of XFF, but not the entries appended by trusted proxies on the RIGHT, so we
    # take the entry `N` from the right. 0 (default) keeps the legacy behaviour
    # (leftmost entry) for backward-compatibility. Set to 1 for direct Cloud Run
    # (the Google Front End appends the real client IP as the rightmost entry) —
    # verify with one request before enabling, since a wrong value would bucket
    # users together. See app/platform/rate_limit.py:_client_key.
    rate_limit_trusted_proxy_hops: int = 0

    # --- Login hardening ---
    # Consecutive failed password attempts before an account is temporarily
    # locked, and how long the lock lasts. Lockout is per-account; the login
    # endpoint is additionally rate-limited per-IP (see rate_limit.py).
    login_max_attempts: int = 5
    login_lockout_seconds: int = 900  # 15 minutes

    # --- Two-factor auth (TOTP) ---
    # Optional Fernet key (urlsafe base64, 32 bytes) used to seal TOTP secrets
    # at rest. When unset, secrets are stored un-encrypted with a startup
    # warning — fine for dev/test, set it in production.
    mfa_secret_key: str = ""
    mfa_issuer: str = "Loupe"

    # --- Apple Sign-In ---
    # Comma-separated list of accepted audiences: the iOS app **bundle id**
    # (native Sign in with Apple) plus any web **Services ID**. A token's `aud`
    # must match one of these. `app.loupe.client` is the current iOS bundle id.
    apple_client_id: str = "com.loupe.app,app.loupe.client"
    apple_jwks_url: str = "https://appleid.apple.com/auth/keys"
    apple_issuer: str = "https://appleid.apple.com"

    @property
    def apple_audiences(self) -> list[str]:
        """Accepted Apple token audiences (bundle id + Services ID), normalised."""
        return [a.strip() for a in self.apple_client_id.split(",") if a.strip()]

    # --- Google Sign-In ---
    google_ios_client_id: str = ""
    google_web_client_id: str = ""
    google_jwks_url: str = "https://www.googleapis.com/oauth2/v3/certs"
    google_issuer: str = "https://accounts.google.com"

    # --- S3 / MinIO ---
    s3_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_bucket: str = "loupe-scans"
    s3_access_key_id: str = "minioadmin"
    s3_secret_access_key: str = "minioadmin"
    s3_presign_expires_seconds: int = 900
    # Dedicated bucket for generated user reports (PDF statements).
    # Falls back to `s3_bucket` when unset so dev / test environments
    # work zero-config; production should always set this to a separate
    # bucket with stricter lifecycle / IAM (reports may contain PII).
    reports_bucket: str | None = None
    # Whether the background scheduler should auto-close monthly /
    # yearly statement cycles. Defaults to enabled; tests and the
    # ``test`` environment turn it off automatically.
    reports_scheduler_enabled: bool = True

    # --- Upstream card catalog APIs ---
    pokemon_tcg_base_url: str = "https://api.pokemontcg.io/v2"
    pokemon_tcg_api_key: str = ""
    scryfall_base_url: str = "https://api.scryfall.com"
    ygoprodeck_base_url: str = "https://db.ygoprodeck.com/api/v7"

    # --- Identification: focus TCG ---
    # The trading-card game the app is currently focused on. When OCR
    # yields no decisive game signal (no HP / ATK-DEF / mana glyph), the
    # identifier biases toward this game *if* there is soft corroborating
    # evidence (see ``identify.confidence._soft_match``) instead of fanning
    # out to every catalog — which is what let a Pokémon card occasionally
    # resolve as Yu-Gi-Oh on a poor read. Set to ``"all"`` to disable the
    # bias entirely once the app is truly multi-game. Scalable: flip this
    # one value (or the ``EXPO``-style env var ``IDENTIFY_PRIMARY_TCG``)
    # as the product's focus shifts; no code changes required.
    identify_primary_tcg: str = "pokemon"

    # --- Pricing APIs (optional) ---
    tcgplayer_client_id: str = ""
    tcgplayer_client_secret: str = ""
    ebay_app_id: str = ""
    ebay_cert_id: str = ""
    pricecharting_api_key: str = ""
    sci_api_key: str = ""

    # --- Billing / Loupe Pro (Stripe; optional, wired later) ---
    # Blank => billing is "not configured": the entitlement layer + paywall
    # still work (admins comp users to Pro from the portal), but the upgrade
    # CTA shows a graceful "launching soon" state instead of a checkout.
    stripe_secret_key: str = ""
    # Publishable key (pk_test_/pk_live_). Safe to expose to the browser — the
    # embedded Payment Element needs it. Served via /me/billing/config.
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    # Stripe Price ids for the two Pro plans the checkout offers.
    stripe_price_pro_monthly: str = ""
    stripe_price_pro_yearly: str = ""
    # Display prices (USD) shown in the paywall. Kept here so marketing copy
    # and the real Stripe Prices stay in sync from one place.
    pro_price_monthly_usd: float = 9.99
    pro_price_yearly_usd: float = 99.0
    # Free trial length (days) applied to every new Pro checkout. 0 disables.
    pro_trial_days: int = 7
    # Where Stripe Checkout returns the user after success/cancel.
    billing_success_url: str = "https://loupe.app/app/settings?upgraded=1"
    billing_cancel_url: str = "https://loupe.app/app/settings"

    # --- Real Data Provider APIs (env-gated; graceful fallback when blank) ---
    # eBay Browse / Marketplace Insights — https://developer.ebay.com
    ebay_oauth_token: str | None = None  # optional pre-minted token
    # PSA Public API — https://www.psacard.com/publicapi
    psa_api_token: str | None = None
    # TCGplayer API — https://docs.tcgplayer.com (mirrors of *_client_*)
    tcgplayer_public_key: str | None = None
    tcgplayer_private_key: str | None = None
    # PriceCharting — https://www.pricecharting.com/api-documentation
    pricecharting_token: str | None = None
    # GoCollect (stub) — https://gocollect.com
    gocollect_api_key: str | None = None
    # JustTCG — https://justtcg.com/api (aggregated TCG prices, free tier)
    justtcg_api_key: str | None = None
    # apitcg — https://www.apitcg.com (catalog for One Piece / Digimon / Dragon
    # Ball / Union Arena / Gundam / Star Wars / Riftbound). One key, many games.
    # Blank disables every apitcg-backed game gracefully (they stay coming-soon).
    apitcg_api_key: str | None = None
    # Hard monthly request ceiling for apitcg (free tier = 1000 req/mo). The
    # catalog layer meters every upstream call against this and serves cached/
    # stale data once the soft ceiling is hit, so we never exceed the plan.
    # Bump this when upgrading the apitcg plan in production.
    apitcg_monthly_budget: int = 1000
    # Monthly ceiling for catalog price-enrichment lookups (the cross-provider
    # market chain, mostly PriceCharting) used to put a $ on catalog-only games
    # (One Piece / Digimon) whose APIs ship no prices. Resolved prices are cached
    # per card for a day, so this is a soft guard against runaway lookups.
    pricechain_monthly_budget: int = 8000
    # PokemonPriceTracker — https://www.pokemonpricetracker.com/api-reference
    # Fills the gap left by the (approval-gated) eBay API: real eBay *sold*
    # data for graded slabs (PSA/CGC/BGS/SGC) plus TCGplayer market price.
    # Free tier ~100 calls/day. Blank disables the provider gracefully.
    pokemonpricetracker_api_key: str | None = None
    # Apify — https://apify.com (Facebook Marketplace nearby-listings scraper).
    # Powers the "Near You" carousel on the card-detail sheet: real FB
    # Marketplace listings for the viewed card, filtered by a radius around
    # the user's device location. Blank disables the feature gracefully.
    apify_api_token: str | None = None
    apify_fb_marketplace_actor: str = "apify/facebook-marketplace-scraper"
    # StockX — https://developer.stockx.com (graded + sealed trading cards)
    # Requires OAuth2 Authorization Code flow; store the refresh_token as a
    # secret and the integration auto-renews the 12h access_token.
    stockx_client_id: str | None = None
    stockx_client_secret: str | None = None
    stockx_api_key: str | None = None  # x-api-key header (from dev portal)
    stockx_refresh_token: str | None = None  # long-lived token from OAuth dance
    # TCGCSV — https://tcgcsv.com (free daily TCGplayer mirror, no key needed).
    # Disabled by default: tcgcsv.com no longer publishes the per-group
    # `products.csv` / `prices.csv` dumps we rely on (every fetch 404s),
    # and the loader synchronously downloads the entire TCGplayer
    # catalog on first request, which can starve the card-detail fan-out
    # within its 4s budget and leave the in-memory cache empty for 6h.
    # Re-enable explicitly via env once a working source/loader is wired.
    tcgcsv_enabled: bool = False

    # --- Google Cloud Platform (production infra) ---
    # Path to service-account JSON. Standard env var the google-* SDKs read
    # automatically; we surface it here for visibility/typing only.
    google_application_credentials: str | None = None
    gcp_project_id: str | None = None
    gcp_region: str = "us-central1"
    # Cloud Storage bucket for scan uploads (replaces S3 when set).
    gcs_bucket: str | None = None
    # Cloud SQL instance connection name, e.g. "loupe-app-56235:us-central1:loupe-pg".
    # Used by the Cloud SQL Auth Proxy for local dev and by Cloud Run in prod.
    cloud_sql_connection_name: str | None = None

    # --- HTTP client tuning ---
    http_timeout_seconds: float = 15.0
    http_max_connections: int = 20
    http_max_keepalive: int = 10

    # --- OCR / card identification pipeline ---
    # Provider selection. ``mock`` returns canned text so dev and CI never
    # touch a paid API; ``google_vision`` calls Cloud Vision text detection
    # ($1.50 per 1k requests after the 1k/mo free tier — leave disabled by
    # default to avoid surprise bills).
    ocr_provider: Literal["mock", "google_vision"] = "mock"
    # Which Vision feature to use. DOCUMENT_TEXT_DETECTION is tuned for
    # dense, structured text (card faces fit that profile better than
    # photos of street signs) and returns block/paragraph structure we
    # can exploit when ranking candidates.
    ocr_google_feature: Literal["TEXT_DETECTION", "DOCUMENT_TEXT_DETECTION"] = (
        "DOCUMENT_TEXT_DETECTION"
    )
    # Hard timeout for any single OCR call. We never let one slow image
    # block a request — on timeout we fall through to phash-only matching.
    ocr_timeout_ms: int = 4_000
    # How many ranked candidates to return on POST /v1/cards/identify.
    ocr_max_candidates: int = 5
    # Sliding window for feedback-driven re-rank boosts. Recent correct
    # confirmations on similar OCR text gently lift those candidates.
    ocr_feedback_boost_window_days: int = 30
    # Maximum bytes accepted on the identify endpoint. Vision API caps at
    # 20 MB; we cap lower to keep round-trip cost predictable.
    ocr_max_image_bytes: int = 8_000_000
    # Preprocess: resize so the longest edge is <= this before sending to
    # the provider. Vision's docs recommend 1024px+ for OCR; oversize images
    # waste bytes without accuracy gain.
    ocr_preprocess_long_edge_px: int = 1600
    # Estimated cost per Google Vision text request, surfaced in admin
    # metrics for spend visibility. Override per pricing tier if needed.
    ocr_google_cost_usd_per_call: float = 0.0015
    # Soft monthly cap on Vision spend. When month-to-date cost_usd hits
    # this number the pipeline refuses to call the paid provider and
    # instead returns ``fallback_required=True`` so the client can run
    # on-device OCR (Apple Vision / ML Kit) and resubmit text via
    # ``POST /v1/cards/identify/text``. Set ≤0 to disable the cap.
    ocr_monthly_budget_usd: float = 50.0

    # --- Perceptual-hash (pHash) image matching ---
    # Master switch for matching a scanned frame's perceptual hash against
    # the catalog's precomputed art hashes. When off, identification falls
    # back to OCR/text only.
    phash_enabled: bool = True
    # Maximum Hamming distance (in bits, over the 256-bit 16x16 hash) for a
    # catalog match to count as a hit. Lower = stricter / fewer false
    # positives; a live photo of a card differs more from clean catalog art
    # than two scans of each other, so we allow a little slack here.
    phash_max_distance: int = 12
    # Fast-path gate: when a fresh scan's pHash is within this (tighter)
    # Hamming distance of a catalog art hash, we trust it as a definite match
    # and return *without* calling the paid/slow OCR provider at all. Must be
    # noticeably stricter than `phash_max_distance` so we only short-circuit on
    # near-certain matches; below it the normal OCR+text+pHash pipeline runs.
    phash_fast_path_max_distance: int = 6

    # --- Observability (optional; no-ops when DSN is missing) ---
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.1
    sentry_profiles_sample_rate: float = 0.1

    # --- AI: natural-language data queries ("Ask your data") ---
    # Optional. When set, the admin "Ask your data" tool uses Claude to turn a
    # question into a read-only SQL SELECT (executed in a read-only transaction
    # with a row cap + statement timeout). Blank => the tool reports "not
    # configured" and never calls out. Defaults to Opus per Anthropic guidance;
    # override with a cheaper model (e.g. claude-sonnet-4-6) for this internal tool.
    anthropic_api_key: str = ""
    nl_query_model: str = "claude-opus-4-8"

    # --- AI: marketplace carousel generation ---
    # Optional. When set, the storefront's discovery shelves are designed by
    # OpenAI (theme + copy + a constrained filter recipe — never the card data),
    # cached one call per game per day. Blank => the web falls back to its
    # curated rotating pool. Set via Secret Manager (`openai-api-key`) in prod.
    openai_api_key: str = ""
    carousel_model: str = "gpt-4o-mini"

    # --- Authorization ---
    # Comma-separated list of email addresses with admin privileges.
    # Pragmatic stand-in until a proper RBAC model lands on `User`.
    # Compared case-insensitively against `User.email`. Empty list means
    # NO admins — admin-gated endpoints will reject every caller.
    admin_emails: str = ""

    @property
    def admin_email_set(self) -> set[str]:
        """Normalised admin email allowlist (lowercased, deduped)."""
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    # --- Email (transactional, via Resend) ---
    # Optional. When `resend_api_key` + `notifications_from_email` are set,
    # applicant status updates are emailed; otherwise they're logged and
    # surfaced only on the public tracking page. Resend's free tier is
    # generous and the API is a single HTTPS POST — no SMTP required.
    resend_api_key: str = ""
    notifications_from_email: str = ""  # e.g. "Loupe Careers <careers@loupe.app>"
    # Public base URL of the web app, used to build links in emails
    # (e.g. the application-tracking page). No trailing slash.
    app_public_url: str = "https://loupe.app"

    @property
    def email_enabled(self) -> bool:
        """True when a transactional-email provider is fully configured."""
        return bool(self.resend_api_key and self.notifications_from_email)

    # --- OpenTelemetry (off by default; flip on once SDK + exporter
    # extras are installed in the runtime image and the IAM role for
    # the target backend is granted).
    # When true, ``init_otel(settings)`` wires FastAPI / SQLAlchemy /
    # HTTPX / Redis instrumentations and pushes spans through the OTLP
    # exporter defined by the standard OTEL_* env vars (Cloud Trace
    # works with the GCP OTLP HTTP endpoint, or use the dedicated
    # opentelemetry-exporter-gcp-trace package).
    otel_enabled: bool = False
    otel_service_name: str = "loupe-backend"
    otel_sample_ratio: float = 0.1

    # --- Convenience flags ---
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` singleton."""
    return Settings()


def reload_settings() -> Settings:
    """Reset the settings cache and return a fresh instance (tests/CI only)."""
    get_settings.cache_clear()
    return get_settings()


__all__ = ["Field", "Settings", "get_settings", "reload_settings"]
