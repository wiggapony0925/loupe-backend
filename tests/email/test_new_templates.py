"""The templates added for the security / dunning / engagement gaps.

The gallery loop in ``test_email_service.py`` already proves every template
renders a well-formed document; these assert the things that are specific to
each one — the numbers a user acts on, the escaping, and (the easy one to get
wrong) which class of mail carries an unsubscribe footer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.services import email_service

_NOW = datetime(2026, 8, 4, 18, 42, tzinfo=UTC)


class _User:
    """Unpersisted stand-in — builders only read these attributes."""

    id = None
    email = "sam@collector.example"
    display_name = "Sam"
    pro_since = datetime(2025, 3, 1, tzinfo=UTC)


# ── Security ──────────────────────────────────────────────────────────────


def test_new_sign_in_reports_the_device_context():
    c = email_service.build_new_sign_in(
        _User(),
        device="iPhone 16 Pro · Safari",
        location="Austin, TX",
        ip="203.0.113.42",
        when=_NOW,
    )
    assert "New sign-in" in c.subject
    for fact in ("iPhone 16 Pro", "Austin, TX", "203.0.113.42", "4 Aug 2026"):
        assert fact in c.html, fact
    # The "wasn't you?" escape hatch is the point of the email.
    assert "wasn't you" in c.text


def test_new_sign_in_degrades_when_context_is_unknown():
    """Not every request carries a parseable UA or geo-IP."""
    c = email_service.build_new_sign_in(_User())
    assert "Unrecognized device" in c.html
    assert "Unknown" in c.html
    assert c.text.strip()


def test_account_locked_states_the_window_and_the_attempts():
    c = email_service.build_account_locked(_User(), minutes=15, attempts=5, when=_NOW)
    assert "15 minutes" in c.html
    assert "5 failed password attempts" in c.html
    # It must be clear nothing was actually compromised.
    assert "Password unchanged" in c.html


def test_account_locked_singular_minute_reads_correctly():
    c = email_service.build_account_locked(_User(), minutes=1, attempts=3)
    assert "1 minute" in c.html
    assert "1 minutes" not in c.html


# ── Billing / dunning ─────────────────────────────────────────────────────


def test_payment_failed_leads_with_the_amount_and_the_retry():
    c = email_service.build_payment_failed(
        _User(),
        amount_usd=Decimal("9.99"),
        attempt=2,
        max_attempts=4,
        next_attempt=datetime(2026, 8, 7, tzinfo=UTC),
        grace_days=11,
    )
    assert "$9.99" in c.html
    assert "2 of 4" in c.html
    assert "7 Aug" in c.html
    assert "11 more days" in c.html
    assert "ACTION NEEDED" in c.html  # the membership card carries the state


def test_payment_failed_without_stripe_extras_still_renders():
    """Stripe omits `next_payment_attempt` on the final attempt."""
    c = email_service.build_payment_failed(_User())
    assert "retry automatically over the next few days" in c.text
    assert "$" not in c.subject
    assert c.html.startswith("<!DOCTYPE html>")


def test_pro_expiring_counts_down_to_the_end_date():
    c = email_service.build_pro_expiring(
        _User(), ends_on=datetime(2026, 8, 20, tzinfo=UTC), days_left=3
    )
    assert "20 August 2026" in c.html
    assert "3 days" in c.subject
    assert "ENDS SOON" in c.html


# ── Engagement ────────────────────────────────────────────────────────────


def test_free_limit_reached_names_the_cap_and_reassures():
    c = email_service.build_free_limit_reached(_User(), card_count=50, limit=50)
    assert "50" in c.subject
    assert "50 of 50 cards used" in c.html
    # Nobody should think their collection was deleted.
    assert "safe and still tracked" in c.text


def test_portfolio_digest_shows_value_move_and_movers():
    c = email_service.build_portfolio_digest(
        _User(),
        period_label="This week",
        total_value_usd=Decimal("12480.50"),
        delta_pct=4.2,
        delta_usd=Decimal("503.20"),
        card_count=147,
        unsub_url="https://loupe.app/u/abc",
        series=[11975, 12040, 12480.50],
        top_movers=[("Charizard ex #125", 12.4), ("Pikachu VMAX", -3.6)],
    )
    assert "$12,480.50" in c.subject
    assert "+4.2%" in c.subject
    assert "Charizard ex #125" in c.html
    assert "+12.4%" in c.html
    assert "-3.6%" in c.html


def test_portfolio_digest_carries_an_unsubscribe_footer():
    """Recurring non-transactional mail must be opt-out-able."""
    c = email_service.build_portfolio_digest(
        _User(),
        period_label="This week",
        total_value_usd=100,
        delta_pct=-1.0,
        delta_usd=-1,
        card_count=1,
        unsub_url="https://loupe.app/u/abc",
    )
    assert "Unsubscribe" in c.html
    assert "https://loupe.app/u/abc" in c.html


def test_transactional_mail_has_no_unsubscribe_footer():
    """The inverse of the rule above — a receipt is not marketing."""
    for c in (
        email_service.build_payment_failed(_User()),
        email_service.build_account_locked(_User(), minutes=15, attempts=5),
        email_service.build_free_limit_reached(_User(), card_count=50, limit=50),
    ):
        assert "Unsubscribe" not in c.html


def test_set_completed_celebrates_with_the_real_count():
    c = email_service.build_set_completed(
        _User(),
        set_name="Evolving Skies",
        set_total=237,
        series_name="Sword & Shield",
        set_id="swsh7",
        total_value_usd=Decimal("4820.00"),
    )
    assert c.subject == "You completed Evolving Skies"
    assert "237 of 237" in c.html
    assert "$4,820.00" in c.html
    assert "/sets/swsh7" in c.html


# ── Escaping (every new template takes user- or catalog-supplied text) ────


def test_new_templates_escape_injected_markup():
    evil = _User()
    evil.display_name = '<script>alert("x")</script>'
    rendered = [
        email_service.build_new_sign_in(evil, device="<img src=x>"),
        email_service.build_account_locked(evil, minutes=15, attempts=5),
        email_service.build_payment_failed(evil),
        email_service.build_pro_expiring(evil, ends_on=_NOW, days_left=1),
        email_service.build_free_limit_reached(evil, card_count=50, limit=50),
        email_service.build_set_completed(evil, set_name="<b>Set</b>", set_total=10),
    ]
    for c in rendered:
        assert "<script>" not in c.html
        assert "&lt;script&gt;" in c.html


def test_digest_escapes_card_names_from_the_catalog():
    c = email_service.build_portfolio_digest(
        _User(),
        period_label="This week",
        total_value_usd=1,
        delta_pct=0.0,
        delta_usd=0,
        card_count=1,
        unsub_url="https://loupe.app/u/abc",
        top_movers=[("<script>bad</script>", 1.0)],
    )
    assert "<script>" not in c.html


def test_every_new_send_wrapper_exists_and_is_exported():
    """Step 3 of the template contract — a builder without a wrapper is dead."""
    for name in (
        "send_new_sign_in",
        "send_account_locked",
        "send_payment_failed",
        "send_pro_expiring",
        "send_free_limit_reached",
        "send_set_completed",
        "send_portfolio_digest",
    ):
        assert hasattr(email_service, name), name
        assert name in email_service.__all__, name
