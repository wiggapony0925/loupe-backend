"""Application configuration via Pydantic settings + .env loading."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed runtime configuration for loupe-backend.

    Values are sourced from environment variables (and optionally a ``.env``
    file in the project root). Access the singleton via :func:`get_settings`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
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

    # --- Apple Sign-In ---
    apple_client_id: str = "com.loupe.app"
    apple_jwks_url: str = "https://appleid.apple.com/auth/keys"
    apple_issuer: str = "https://appleid.apple.com"

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

    # --- Pricing APIs (optional) ---
    tcgplayer_client_id: str = ""
    tcgplayer_client_secret: str = ""
    ebay_app_id: str = ""
    ebay_cert_id: str = ""
    pricecharting_api_key: str = ""
    sci_api_key: str = ""

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
    # TCGCSV — https://tcgcsv.com (free daily TCGplayer mirror, no key needed).
    # Set to false to disable the background download.
    tcgcsv_enabled: bool = True

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

    # --- Observability (optional; no-ops when DSN is missing) ---
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.1
    sentry_profiles_sample_rate: float = 0.1

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
