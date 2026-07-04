"""Announcement-class templates (blog, admin-composed) + support messages.

Blog posts render like the blog itself — tag chip, cover, serif title,
byline. Composed announcements carry the megaphone eyebrow. Support messages
read like a letter from a person: greeting heading, highlighted reply note,
signed by the team. Announcements carry the unsubscribe footer; support is
one-to-one transactional and deliberately doesn't.
"""

from __future__ import annotations

from app.services.email_templates import theme
from app.services.email_templates.base import (
    EmailContent,
    app_url,
    callout,
    chip,
    esc,
    paragraphs,
    render_email,
)


def unsubscribe_footer(unsub_url: str) -> str:
    """The CAN-SPAM footer every announcement-class email must carry."""
    return (
        f'<p style="{theme.FINE_PRINT_STYLE}">'
        f"You're receiving product updates because you have a Loupe "
        f'account. <a href="{esc(unsub_url)}" target="_blank" '
        f'style="color:{theme.INK_DIM};text-decoration:underline;">'
        f"Unsubscribe</a> from these emails.</p>"
    )


def build_blog_announcement(
    *,
    title: str,
    excerpt: str,
    slug: str,
    unsub_url: str,
    cover_image_url: str | None = None,
    tag: str | None = None,
    author: str | None = None,
    read_minutes: int | None = None,
) -> EmailContent:
    url = f"{app_url()}/blog/{slug}"
    cover = (
        f'<a href="{url}" target="_blank">'
        f'<img src="{esc(cover_image_url)}" alt="{esc(title)}" '
        f'width="{theme.CONTENT_WIDTH}" '
        f'style="display:block;width:100%;height:auto;border-radius:12px;'
        f'margin:0 0 16px;border:1px solid {theme.LINE};"></a>'
        if cover_image_url
        else ""
    )
    byline_bits = [
        b for b in (author, f"{read_minutes} min read" if read_minutes else None) if b
    ]
    byline = (
        f'<p style="margin:2px 0 14px;font-size:13px;color:{theme.INK_DIM};">'
        f"{esc(' · '.join(byline_bits))}</p>"
        if byline_bits
        else ""
    )
    body = (
        f"{cover}{byline}"
        f"<p>{esc(excerpt) or 'A new post is live on the Loupe blog.'}</p>"
    )
    html, text = render_email(
        esc(title),
        body,
        ("Read the post", url),
        preheader=excerpt or "A new post is live on the Loupe blog.",
        footer_html=unsubscribe_footer(unsub_url),
        eyebrow=f"From the blog · {tag}" if tag else "From the blog",
    )
    return EmailContent(f"New from Loupe: {title}", html, text)


def build_custom_announcement(
    *,
    subject: str,
    heading: str,
    body_text: str,
    cta: tuple[str, str] | None,
    unsub_url: str,
) -> EmailContent:
    html, text = render_email(
        esc(heading),
        paragraphs(body_text),
        cta,
        preheader=body_text.strip().splitlines()[0] if body_text.strip() else "",
        footer_html=unsubscribe_footer(unsub_url),
        eyebrow="Announcement",
    )
    return EmailContent(subject, html, text)


def build_support_message(
    *,
    recipient_name: str | None,
    subject: str,
    body_text: str,
    cta: tuple[str, str] | None = None,
) -> EmailContent:
    """A one-to-one message from the support team. Transactional (a human
    wrote it to this user), so no unsubscribe footer — and 'just reply'
    works because sends carry the support reply-to/from address."""
    greeting = f"Hi {esc(recipient_name)}," if recipient_name else "Hi there,"
    signature = (
        f'<p style="margin:18px 0 0;font-size:14px;color:{theme.INK};">'
        f"&mdash; Loupe Support&nbsp;{chip('a real human', tone='mint')}</p>"
    )
    body = (
        f"{paragraphs(body_text)}"
        + callout(
            "Questions? Just reply to this email &mdash; it reaches a human "
            "on the Loupe team.",
            tone="mint",
        )
        + signature
    )
    html, text = render_email(
        greeting,
        body,
        cta,
        preheader=body_text.strip().splitlines()[0] if body_text.strip() else "",
        eyebrow="From support",
    )
    return EmailContent(subject, html, text)


__all__ = [
    "build_blog_announcement",
    "build_custom_announcement",
    "build_support_message",
    "unsubscribe_footer",
]
