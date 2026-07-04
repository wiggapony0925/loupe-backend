"""Unit tests for the transactional-email layer (rendering + provider I/O).

No real HTTP: provider calls are exercised through a fake httpx client so we
can assert retry behavior, payload shape, and headers without network.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import get_settings
from app.services import email_service
from app.services.admin import email_preview_service

# ── Rendering ─────────────────────────────────────────────────────────────


def test_render_email_is_a_full_document_with_preheader():
    html, text = email_service.render_email(
        "Hi Sam,",
        "<p>Body copy.</p>",
        ("Open Loupe", "https://loupe.app"),
        preheader="Preview line.",
    )
    assert html.startswith("<!DOCTYPE html>")
    assert '<html lang="en"' in html
    assert 'name="color-scheme"' in html  # dark-mode hint for mail clients
    assert "Preview line." in html  # hidden preheader
    assert "display:none" in html
    # Plain-text part mirrors the content and carries the CTA URL.
    assert "Hi Sam," in text
    assert "Body copy." in text
    assert "Open Loupe: https://loupe.app" in text


def test_render_email_escapes_nothing_twice_but_text_unescapes():
    html, text = email_service.render_email("Hi,", "<p>Fees &amp; taxes</p>")
    assert "Fees &amp; taxes" in html
    assert "Fees & taxes" in text  # entities resolved in the text part


def test_user_content_is_escaped_in_templates():
    class Evil:
        email = "evil@example.com"
        display_name = '<script>alert("x")</script>'

    content = email_service.build_welcome(Evil())
    assert "<script>" not in content.html
    assert "&lt;script&gt;" in content.html


def test_every_gallery_template_renders():
    for spec in email_preview_service.TEMPLATES:
        content = spec.render()
        assert content.subject, spec.key
        assert content.html.startswith("<!DOCTYPE html>"), spec.key
        assert content.text.strip(), spec.key


def test_blog_announcement_contains_unsubscribe_link():
    content = email_service.build_blog_announcement(
        title="Hello",
        excerpt="World",
        slug="hello",
        unsub_url="https://api.test/v1/public/unsubscribe?token=t",
    )
    assert "unsubscribe?token=t" in content.html
    assert "Unsubscribe" in content.html
    assert "unsubscribe?token=t" in content.text


def test_blog_announcement_renders_cover_image_when_present():
    with_cover = email_service.build_blog_announcement(
        title="T",
        excerpt="E",
        slug="t",
        unsub_url="https://u.test/u",
        cover_image_url="https://cdn.test/cover.jpg",
    )
    assert '<img src="https://cdn.test/cover.jpg"' in with_cover.html
    without = email_service.build_blog_announcement(
        title="T", excerpt="E", slug="t", unsub_url="https://u.test/u"
    )
    assert "<img" not in without.html


def test_price_alert_formats_money_and_direction():
    content = email_service.build_price_alert(
        card_name="Charizard ex #199",
        set_name="Obsidian Flames",
        condition="below",
        threshold_usd=Decimal("1250"),
        price_usd=Decimal("1199.5"),
        card_id="abc",
    )
    assert "dropped below" in content.html
    assert "$1,250.00" in content.html
    assert content.subject == "Charizard ex #199 is now $1,199.50"


# ── Provider I/O ──────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = f"status {status_code}"


class _FakeClient:
    """Stands in for httpx.AsyncClient; pops one scripted status per POST."""

    calls: list[dict] = []
    script: list[int] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse(_FakeClient.script.pop(0))


@pytest.fixture
def provider(monkeypatch):
    """Enable the provider and install the fake HTTP client."""
    settings = get_settings()
    monkeypatch.setattr(settings, "resend_api_key", "re_test", raising=False)
    monkeypatch.setattr(
        settings, "notifications_from_email", "Loupe <hello@test>", raising=False
    )
    monkeypatch.setattr(email_service.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(email_service, "_RETRY_DELAY_SEC", 0)
    _FakeClient.calls = []
    _FakeClient.script = []
    return _FakeClient


@pytest.mark.asyncio
async def test_send_email_is_a_noop_without_a_provider():
    assert get_settings().email_enabled is False  # test env has no key
    ok = await email_service.send_email("a@b.co", "s", "<p>h</p>")
    assert ok is False


@pytest.mark.asyncio
async def test_send_email_success_payload(provider):
    provider.script = [200]
    ok = await email_service.send_email(
        "a@b.co", "Subject", "<p>h</p>", "h", category="welcome"
    )
    assert ok is True
    call = provider.calls[0]
    assert call["json"]["to"] == ["a@b.co"]
    assert call["json"]["text"] == "h"
    assert call["json"]["tags"] == [{"name": "category", "value": "welcome"}]
    assert call["headers"]["Authorization"] == "Bearer re_test"


@pytest.mark.asyncio
async def test_send_email_retries_transient_then_succeeds(provider):
    provider.script = [429, 200]
    ok = await email_service.send_email("a@b.co", "s", "<p>h</p>")
    assert ok is True
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_send_email_does_not_retry_hard_errors(provider):
    provider.script = [422]
    ok = await email_service.send_email("a@b.co", "s", "<p>h</p>")
    assert ok is False
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_idempotency_key_header_is_sent(provider):
    provider.script = [200]
    await email_service.send_email(
        "a@b.co", "s", "<p>h</p>", idempotency_key="pro-activated-sub_1"
    )
    assert provider.calls[0]["headers"]["Idempotency-Key"] == "pro-activated-sub_1"


@pytest.mark.asyncio
async def test_reply_to_comes_from_settings(provider, monkeypatch):
    monkeypatch.setattr(
        get_settings(), "notifications_reply_to", "support@test", raising=False
    )
    provider.script = [200]
    await email_service.send_email("a@b.co", "s", "<p>h</p>")
    assert provider.calls[0]["json"]["reply_to"] == "support@test"


@pytest.mark.asyncio
async def test_send_batch_chunks_at_100(provider):
    provider.script = [200, 200, 200]
    messages = [
        email_service._payload(
            f"u{i}@t.co", "s", "<p>h</p>", "h", category=None, headers=None
        )
        for i in range(250)
    ]
    accepted = await email_service.send_batch(messages)
    assert accepted == 250
    assert [len(c["json"]) for c in provider.calls] == [100, 100, 50]
    assert all(c["url"].endswith("/emails/batch") for c in provider.calls)


@pytest.mark.asyncio
async def test_blog_announcement_sets_one_click_unsubscribe_headers(provider):
    provider.script = [200]
    sent = await email_service.send_blog_announcement(
        [
            ("a@b.co", "https://api.test/u?token=t1"),
            ("c@d.co", "https://api.test/u?token=t2"),
        ],
        title="Post",
        excerpt="",
        slug="post",
    )
    assert sent == 2
    batch = provider.calls[0]["json"]
    assert batch[0]["headers"]["List-Unsubscribe"] == "<https://api.test/u?token=t1>"
    assert batch[0]["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert batch[1]["headers"]["List-Unsubscribe"] == "<https://api.test/u?token=t2>"
