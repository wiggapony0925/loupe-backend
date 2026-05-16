"""Settings & config loading checks."""

from app.config import get_settings, reload_settings


def test_settings_singleton():
    assert get_settings() is get_settings()


def test_settings_test_env():
    s = reload_settings()
    assert s.app_env == "test"
    assert s.database_url.startswith("sqlite")
