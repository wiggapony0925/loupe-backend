"""Tests for the Google Secret Manager settings source.

These never touch GCP — they exercise the gating logic and the env-precedence
contract with a fake Secret Manager module injected via sys.modules.
"""

import sys
import types

import pytest

from app.platform import secret_manager as sm


@pytest.fixture
def fake_secret_manager(monkeypatch):
    """Inject a fake google.cloud.secretmanager whose secrets we control."""
    store: dict[str, str] = {}

    class _FakeClient:
        def access_secret_version(self, name: str):
            # name = projects/<proj>/secrets/<ID>/versions/latest
            secret_id = name.split("/secrets/", 1)[1].split("/", 1)[0]
            if secret_id not in store:
                raise RuntimeError("NotFound")
            payload = types.SimpleNamespace(data=store[secret_id].encode("utf-8"))
            return types.SimpleNamespace(payload=payload)

    fake_mod = types.SimpleNamespace(SecretManagerServiceClient=lambda: _FakeClient())
    google_cloud = types.ModuleType("google.cloud")
    google_cloud.secretmanager = fake_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_mod)
    return store


def test_noop_outside_production(monkeypatch, fake_secret_manager):
    monkeypatch.setenv("APP_ENV", "development")
    fake_secret_manager["STRIPE_SECRET_KEY"] = "sk_live_should_not_load"
    assert sm._load_secrets() == {}


def test_disabled_via_escape_hatch(monkeypatch, fake_secret_manager):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("GCP_PROJECT_ID", "loupe-app")
    monkeypatch.setenv("LOUPE_DISABLE_SECRET_MANAGER", "1")
    fake_secret_manager["STRIPE_SECRET_KEY"] = "sk_live_x"
    assert sm._load_secrets() == {}


def test_loads_unset_secrets_in_production(monkeypatch, fake_secret_manager):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("GCP_PROJECT_ID", "loupe-app")
    monkeypatch.delenv("LOUPE_DISABLE_SECRET_MANAGER", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    fake_secret_manager["STRIPE_SECRET_KEY"] = "sk_live_from_sm"
    fake_secret_manager["ANTHROPIC_API_KEY"] = "sk-ant-from-sm"

    loaded = sm._load_secrets()
    assert loaded["stripe_secret_key"] == "sk_live_from_sm"
    assert loaded["anthropic_api_key"] == "sk-ant-from-sm"


def test_env_var_wins_over_secret_manager(monkeypatch, fake_secret_manager):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("GCP_PROJECT_ID", "loupe-app")
    monkeypatch.delenv("LOUPE_DISABLE_SECRET_MANAGER", raising=False)
    # The env var is already set → the source must skip it entirely.
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_from_env")
    fake_secret_manager["STRIPE_SECRET_KEY"] = "sk_live_from_sm"

    loaded = sm._load_secrets()
    assert "stripe_secret_key" not in loaded


def test_missing_secret_is_skipped_gracefully(monkeypatch, fake_secret_manager):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("GCP_PROJECT_ID", "loupe-app")
    monkeypatch.delenv("LOUPE_DISABLE_SECRET_MANAGER", raising=False)
    # No secrets in the store → every lookup raises → empty result, no error.
    assert sm._load_secrets() == {}


def test_no_project_resolved_is_noop(monkeypatch, fake_secret_manager):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.delenv("CLOUD_SQL_CONNECTION_NAME", raising=False)
    monkeypatch.setattr(sm, "_resolve_project", lambda: None)
    fake_secret_manager["STRIPE_SECRET_KEY"] = "x"
    assert sm._load_secrets() == {}
