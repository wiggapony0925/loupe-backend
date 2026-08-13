"""Outside production, a configured mail key is not consent to send.

WHY. ``.env`` carries a real Resend key so the send path can be exercised
deliberately, and ``email_enabled`` used to be ``key and from_address`` — so a
developer machine, a test run against a real .env, or a CI job with the secret
present would all happily send. It nearly happened: a fuzzing pass over the
public endpoints created 23 users and a waitlist entry, every one of which
sends mail, and nothing stopped it except the key being blanked by hand first.

Production behaviour is unchanged. Everywhere else has to say
``EMAIL_SEND_OUTSIDE_PRODUCTION=true``, which is a thing you type on purpose.
"""

from __future__ import annotations

import pytest

from app.config import Settings


def _settings(**overrides) -> Settings:
    base = {
        "resend_api_key": "re_test_key_not_real",
        "notifications_from_email": "Loupe <hello@example.test>",
    }
    return Settings(**{**base, **overrides})


@pytest.mark.parametrize("env", ["development", "staging"])
def test_a_configured_key_does_not_send_in_dev_or_staging(env):
    """The regression. Fully configured, and still refuses."""
    assert _settings(app_env=env).email_enabled is False


@pytest.mark.parametrize("env", ["development", "staging"])
def test_sending_in_dev_or_staging_requires_saying_so(env):
    assert (
        _settings(app_env=env, email_send_outside_production=True).email_enabled is True
    )


def test_the_test_environment_is_not_gated():
    """Deliberate, and worth pinning so nobody "tightens" it back.

    The risk is a real credential plus a real transport. pytest has neither —
    the root conftest blanks RESEND_API_KEY, and the delivery suites stub
    httpx.AsyncClient. Gating it would make every one of those tests opt in to
    reach a fake, which teaches setting the flag by reflex.
    """
    assert _settings(app_env="test").email_enabled is True


def test_production_still_sends_without_any_extra_flag():
    """The guard must not become a thing someone has to remember in prod."""
    assert _settings(app_env="production").email_enabled is True


def test_production_sends_regardless_of_the_opt_in_value():
    """The flag is a non-production safety catch, not a kill switch."""
    assert (
        _settings(
            app_env="production", email_send_outside_production=False
        ).email_enabled
        is True
    )


@pytest.mark.parametrize("env", ["development", "production"])
def test_missing_configuration_still_wins_over_everything(env):
    """No key means no mail, opt-in or not, production or not."""
    assert (
        _settings(
            app_env=env, resend_api_key="", email_send_outside_production=True
        ).email_enabled
        is False
    )
    assert (
        _settings(
            app_env=env,
            notifications_from_email="",
            email_send_outside_production=True,
        ).email_enabled
        is False
    )
