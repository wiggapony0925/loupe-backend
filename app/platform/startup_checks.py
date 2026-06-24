"""Fail-fast production configuration validation.

The app degrades to convenient dev defaults when secrets are unset (ephemeral
JWT keys, plaintext-sealed TOTP, the bundled sqlite DB, open API docs). Those
defaults are *fine for development* but must never run in production — so this
module turns "silent insecure fallback" into a loud, actionable boot error.

Called once from :func:`app.main.create_app`. A no-op unless
``APP_ENV=production``.
"""

from __future__ import annotations

import os

from app.config import Settings
from app.utils.logger import get_logger

logger = get_logger("startup.checks")


class ProductionConfigError(RuntimeError):
    """Raised at boot when production is configured insecurely."""


def validate_production_config(settings: Settings) -> None:
    """Refuse to boot in production on a *fatal* misconfig; warn on softer gaps.

    Fatal (raise — these are also functionally broken in a multi-instance
    deployment, not just insecure):
      * JWT signing keys unset → each instance mints an *ephemeral* RSA key, so
        tokens signed by one pod are rejected by the next and every restart
        invalidates all sessions. There is no real signing root.
      * ``DATABASE_URL`` still the bundled sqlite default → no shared, durable
        store.

    Warn (log ``CRITICAL`` — insecure but the app still functions):
      * ``MFA_SECRET_KEY`` unset → TOTP secrets sealed in plaintext
        (:func:`app.auth.mfa.seal_secret` additionally refuses to enroll new
        secrets in production).
      * ``ADMIN_EMAILS`` empty → nobody can reach the developer portal.
      * ``DOCS_ACCESS_TOKEN`` unset → the OpenAPI spec / docs are world-readable.
      * S3 credentials left at the ``minioadmin`` dev defaults.
    """
    if settings.app_env != "production":
        return

    fatal: list[str] = []
    warn: list[str] = []

    if not settings.jwt_private_key_pem.strip() or not settings.jwt_public_key_pem.strip():
        fatal.append(
            "JWT_PRIVATE_KEY_PEM / JWT_PUBLIC_KEY_PEM must be set in production "
            "(ephemeral per-process keys break auth across instances)."
        )
    if settings.database_url.strip().startswith("sqlite"):
        fatal.append(
            "DATABASE_URL must point at the production database, not the bundled "
            "sqlite default."
        )

    if not settings.mfa_secret_key.strip():
        warn.append(
            "MFA_SECRET_KEY is unset — TOTP secrets would be stored unencrypted. "
            "Set a Fernet key (new MFA enrollments are blocked until you do)."
        )
    if not settings.admin_email_set:
        warn.append("ADMIN_EMAILS is empty — no account can access the developer portal.")
    if not os.environ.get("DOCS_ACCESS_TOKEN", "").strip():
        warn.append(
            "DOCS_ACCESS_TOKEN is unset — the OpenAPI spec and API docs are "
            "publicly reachable."
        )
    if "minioadmin" in (settings.s3_access_key_id, settings.s3_secret_access_key):
        warn.append("S3 credentials are still the insecure 'minioadmin' dev defaults.")

    for w in warn:
        logger.critical("INSECURE PRODUCTION CONFIG: %s", w)

    if fatal:
        bullets = "\n  - ".join(fatal)
        raise ProductionConfigError(
            "Refusing to start: insecure production configuration:\n  - " + bullets
        )


__all__ = ["ProductionConfigError", "validate_production_config"]
