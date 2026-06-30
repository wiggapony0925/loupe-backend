"""Unit tests for the admin environment manager service.

The cardinal rule: a secret's value must NEVER appear in the report. Non-secret
config is surfaced verbatim so the portal can show it.
"""

from app.config import Settings
from app.services.admin import env_service


def _make_settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


def test_report_groups_and_app_env():
    settings = _make_settings(app_env="production")
    report = env_service.report(settings)
    assert report.app_env == "production"
    assert len(report.variables) > 20
    # Every variable is well-formed.
    for v in report.variables:
        assert v.key and v.label and v.group and v.description
        assert v.length >= 0


def test_secret_value_is_never_echoed():
    secret = "sk_live_super_secret_value_1234567890"
    settings = _make_settings(stripe_secret_key=secret, anthropic_api_key="xyz")
    report = env_service.report(settings)
    by_key = {v.key: v for v in report.variables}

    stripe = by_key["STRIPE_SECRET_KEY"]
    assert stripe.secret is True
    assert stripe.is_set is True
    assert stripe.value is None  # withheld
    assert stripe.length == len(secret)  # presence/length only

    # The raw secret must not leak anywhere in the serialized report.
    blob = report.model_dump_json()
    assert secret not in blob


def test_unset_secret_reports_absent():
    settings = _make_settings(stripe_secret_key="")
    stripe = next(
        v
        for v in env_service.report(settings).variables
        if v.key == "STRIPE_SECRET_KEY"
    )
    assert stripe.is_set is False
    assert stripe.length == 0
    assert stripe.value is None


def test_non_secret_value_is_surfaced():
    pk = "pk_live_browser_safe_key"
    settings = _make_settings(
        stripe_publishable_key=pk,
        gcp_region="europe-west1",
        nl_query_model="claude-opus-4-8",
    )
    by_key = {v.key: v for v in env_service.report(settings).variables}

    pub = by_key["STRIPE_PUBLISHABLE_KEY"]
    assert pub.secret is False
    assert pub.value == pk  # publishable key is browser-safe → shown

    assert by_key["GCP_REGION"].value == "europe-west1"
    assert by_key["NL_QUERY_MODEL"].value == "claude-opus-4-8"


def test_bool_and_default_values_render():
    settings = _make_settings(log_json=True)
    by_key = {v.key: v for v in env_service.report(settings).variables}
    assert by_key["LOG_JSON"].value == "true"
    assert by_key["LOG_JSON"].is_set is True
    # A non-secret with a non-empty default still shows as set.
    assert by_key["APP_ENV"].is_set is True


def test_docs_links_present_for_providers():
    by_key = {v.key: v for v in env_service.report(_make_settings()).variables}
    assert by_key["RESEND_API_KEY"].docs_url == "https://resend.com/api-keys"
    assert by_key["ANTHROPIC_API_KEY"].docs_url == "https://console.anthropic.com"
