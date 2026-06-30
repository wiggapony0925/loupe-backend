"""Google Cloud Secret Manager as a pydantic-settings source.

In production, this hydrates any **unset** secret from the Secret Manager you
already run in GCP — so deploying to Cloud Run with the service account granted
``roles/secretmanager.secretAccessor`` lights up Stripe, Resend, Anthropic, the
provider keys, etc. without pasting them into env vars.

Precedence (wired in :class:`app.config.Settings`): explicit env vars still win;
Secret Manager only fills the gaps. The secret **id** is the upper-snake env name
(e.g. ``STRIPE_SECRET_KEY``) and the latest version is read.

Hard-gated + fully graceful: it does nothing unless ``APP_ENV=production`` AND
the ``google-cloud-secret-manager`` library is installed AND a project resolves.
Any per-secret failure (missing secret, no permission) is skipped silently, so a
partial setup never breaks startup. Set ``LOUPE_DISABLE_SECRET_MANAGER=1`` to
turn it off entirely.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import PydanticBaseSettingsSource

from app.utils.logger import get_logger

logger = get_logger("config.secret_manager")

# Settings fields backed by Secret Manager. The secret id in GCP is the
# upper-snake form of the field name (= the env var name).
_SECRET_FIELDS: tuple[str, ...] = (
    "database_url",
    "redis_url",
    "jwt_private_key_pem",
    "jwt_public_key_pem",
    "mfa_secret_key",
    "stripe_secret_key",
    "stripe_webhook_secret",
    "resend_api_key",
    "anthropic_api_key",
    "pokemon_tcg_api_key",
    "apitcg_api_key",
    "pricecharting_token",
    "pricecharting_api_key",
    "tcgplayer_client_id",
    "tcgplayer_client_secret",
    "ebay_app_id",
    "ebay_cert_id",
    "ebay_oauth_token",
    "justtcg_api_key",
    "pokemonpricetracker_api_key",
    "psa_api_token",
    "gocollect_api_key",
    "stockx_client_id",
    "stockx_client_secret",
    "stockx_api_key",
    "stockx_refresh_token",
    "apify_api_token",
    "sci_api_key",
    "sentry_dsn",
    "s3_access_key_id",
    "s3_secret_access_key",
)


def _resolve_project() -> str | None:
    """Resolve the GCP project without importing Settings (avoids recursion)."""
    explicit = os.getenv("GCP_PROJECT_ID")
    if explicit:
        return explicit
    sql = os.getenv("CLOUD_SQL_CONNECTION_NAME", "")
    if ":" in sql:
        return sql.split(":", 1)[0]
    try:
        import google.auth

        _, project = google.auth.default()
        return project
    except Exception:
        return None


def _load_secrets() -> dict[str, Any]:
    """Fetch unset secrets from Secret Manager. Returns {} when not applicable."""
    if os.getenv("APP_ENV", "development").lower() != "production":
        return {}
    if os.getenv("LOUPE_DISABLE_SECRET_MANAGER"):
        return {}
    try:
        from google.cloud import secretmanager
    except Exception:
        logger.info("google-cloud-secret-manager not installed — skipping.")
        return {}

    project = _resolve_project()
    if not project:
        logger.warning("Secret Manager: no GCP project resolved — skipping.")
        return {}

    try:
        client = secretmanager.SecretManagerServiceClient()
    except Exception as exc:  # pragma: no cover - needs real ADC
        logger.warning("Secret Manager client init failed: %s", type(exc).__name__)
        return {}

    out: dict[str, Any] = {}
    loaded: list[str] = []
    for field in _SECRET_FIELDS:
        secret_id = field.upper()
        # Explicit env wins — never override it, and skip the network round-trip.
        if os.getenv(secret_id):
            continue
        name = f"projects/{project}/secrets/{secret_id}/versions/latest"
        try:
            resp = client.access_secret_version(name=name)
            value = resp.payload.data.decode("utf-8")
        except Exception:
            continue  # missing secret / no access — leave field to its default
        if value:
            out[field] = value
            loaded.append(secret_id)

    if loaded:
        logger.info(
            "Secret Manager: hydrated %d secret(s): %s", len(loaded), ", ".join(loaded)
        )
    return out


class GoogleSecretManagerSource(PydanticBaseSettingsSource):
    """A pydantic-settings source that fills unset secrets from Secret Manager."""

    def __init__(self, settings_cls: type) -> None:
        super().__init__(settings_cls)
        self._values: dict[str, Any] = _load_secrets()

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._values)


__all__ = ["GoogleSecretManagerSource"]
