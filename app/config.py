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

    # --- HTTP client tuning ---
    http_timeout_seconds: float = 15.0
    http_max_connections: int = 20
    http_max_keepalive: int = 10

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
