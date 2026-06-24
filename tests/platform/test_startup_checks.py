"""Production configuration guard + MFA prod-seal hardening."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.platform.startup_checks import (
    ProductionConfigError,
    validate_production_config,
)


def _prod(**overrides) -> Settings:
    base = {
        "app_env": "production",
        "jwt_private_key_pem": "PRIVATE",
        "jwt_public_key_pem": "PUBLIC",
        "database_url": "postgresql+asyncpg://u:p@db/loupe",
        "mfa_secret_key": "a-key",
        "admin_emails": "admin@loupe.app",
        "s3_access_key_id": "real-key",
        "s3_secret_access_key": "real-secret",
    }
    base.update(overrides)
    return Settings(**base)


def test_noop_outside_production():
    # development/test never raise, whatever the config.
    validate_production_config(Settings(app_env="development", jwt_private_key_pem=""))
    validate_production_config(
        Settings(app_env="test", database_url="sqlite+aiosqlite://")
    )


def test_fully_configured_production_boots():
    validate_production_config(_prod())  # no raise


def test_raises_without_jwt_keys():
    with pytest.raises(ProductionConfigError, match="JWT"):
        validate_production_config(_prod(jwt_private_key_pem="", jwt_public_key_pem=""))


def test_raises_on_sqlite_db():
    with pytest.raises(ProductionConfigError, match="DATABASE_URL"):
        validate_production_config(_prod(database_url="sqlite+aiosqlite:///./loupe.db"))


def test_soft_gaps_warn_but_do_not_block_boot(caplog):
    # MFA/admin unset are insecure but non-fatal: the app still boots.
    with caplog.at_level("CRITICAL"):
        validate_production_config(_prod(mfa_secret_key="", admin_emails=""))
    joined = " ".join(r.message for r in caplog.records)
    assert "MFA_SECRET_KEY" in joined
    assert "ADMIN_EMAILS" in joined


def test_seal_secret_refuses_plaintext_in_production(monkeypatch):
    from app.auth import mfa

    monkeypatch.setattr(
        mfa, "get_settings", lambda: Settings(app_env="production", mfa_secret_key="")
    )
    with pytest.raises(RuntimeError, match="MFA_SECRET_KEY"):
        mfa.seal_secret("JBSWY3DPEHPK3PXP")


def test_seal_secret_allows_plaintext_in_dev(monkeypatch):
    from app.auth import mfa

    monkeypatch.setattr(
        mfa, "get_settings", lambda: Settings(app_env="development", mfa_secret_key="")
    )
    assert mfa.seal_secret("JBSWY3DPEHPK3PXP").startswith("p:")
