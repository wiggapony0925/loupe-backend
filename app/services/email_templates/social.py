"""Community mail: followers, follow requests, and profile likes.

The social family is the one place Loupe emails you about *another person*,
so every template leads with who they are and links straight to their
profile — the only two things you need to decide whether you care.

Privacy shapes this set. A public account gets "started following you" (it
already happened); a private account gets "asked to follow you" (it hasn't,
and only they can approve it). Those are different emails because they ask
for different things, and sending the public one to a private account would
imply access that hasn't been granted.
"""

from __future__ import annotations

from app.services.email_templates import theme
from app.services.email_templates.base import (
    EmailContent,
    app_url,
    callout,
    chip,
    esc,
    panel,
    render_email,
)


def _profile_url(username: str) -> str:
    return f"{app_url()}/app/u/{username}"


def _actor_tile(display_name: str, username: str, subtitle: str) -> str:
    """The dark 'who' tile — an avatar-less identity card that renders the
    same in every client (remote avatars are often blocked, and a broken
    image where a face should be reads worse than no image)."""
    initial = esc((display_name or username or "?").strip()[:1].upper())
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:18px 0 8px;"><tr>'
        f'<td align="center" style="background:{theme.BAND_BG};border-radius:14px;'
        f'padding:24px 20px;">'
        f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
        f'align="center"><tr><td align="center" style="width:56px;height:56px;'
        f"background:{theme.BAND_MINT};border-radius:999px;color:{theme.BAND_BG};"
        f"font-size:24px;font-weight:700;font-family:{theme.FONT_SERIF};"
        f'line-height:56px;">{initial}</td></tr></table>'
        f'<p style="margin:14px 0 0;font-size:19px;font-weight:600;color:#f5f5f7;'
        f'letter-spacing:-0.01em;font-family:{theme.FONT_SERIF};">'
        f"{esc(display_name or username)}</p>"
        f'<p style="margin:6px 0 0;font-size:12px;color:{theme.BAND_MUTED};'
        f'font-family:{theme.FONT_MONO};">@{esc(username)}</p>'
        f'<p style="margin:10px 0 0;font-size:10px;font-weight:700;'
        f"letter-spacing:0.16em;text-transform:uppercase;color:{theme.BAND_MUTED};"
        f'font-family:{theme.FONT};">{esc(subtitle)}</p>'
        f"</td></tr></table>"
    )


def build_new_follower(
    *,
    follower_name: str,
    follower_username: str,
    follower_collection_count: int | None = None,
    is_private: bool = False,
) -> EmailContent:
    """Someone started following you — a public-account event.

    For a private account the equivalent moment is a *request*, which is a
    different email; ``is_private`` here describes the FOLLOWER's account, and
    only changes the hint about whether you can follow them back.
    """
    body = (
        _actor_tile(follower_name, follower_username, "New follower")
        + f"<p><strong>{esc(follower_name or follower_username)}</strong> is now "
        "following your collection.</p>"
        + (
            panel([("Their collection", f"{follower_collection_count} cards")])
            if follower_collection_count is not None
            else ""
        )
        + callout(
            "Their profile is private — following back sends a request they'll "
            "need to approve."
            if is_private
            else "Have a look at what they collect, and follow back if you like "
            "what you see.",
            tone="neutral" if is_private else "mint",
        )
    )
    html, text = render_email(
        "You have a new follower.",
        body,
        ("View their profile", _profile_url(follower_username)),
        preheader=f"{follower_name or follower_username} started following you.",
        eyebrow="Community",
    )
    return EmailContent(
        f"{follower_name or follower_username} started following you", html, text
    )


def build_follow_request(
    *, requester_name: str, requester_username: str
) -> EmailContent:
    """Someone asked to follow your private account.

    Higher urgency than a plain follow: nothing happens until the owner acts,
    so this is the one social email with a decision in it.
    """
    body = (
        _actor_tile(requester_name, requester_username, "Wants to follow you")
        + f"<p><strong>{esc(requester_name or requester_username)}</strong> asked "
        "to follow your collection. Because your profile is private, they "
        "can't see it until you approve.</p>"
        + f'<p style="margin:14px 0 4px;text-align:center;">'
        f"{chip('Awaiting your approval', tone='amber')}</p>"
        + callout(
            "Approve and they'll see what you've chosen to share. Decline and "
            "they're not told — the request simply disappears.",
            tone="neutral",
        )
    )
    html, text = render_email(
        "Someone wants to follow you.",
        body,
        ("Review the request", f"{app_url()}/app/community/requests"),
        preheader=(
            f"{requester_name or requester_username} asked to follow your collection."
        ),
        eyebrow="Follow request",
        eyebrow_color="#b8860b",
    )
    return EmailContent(
        f"{requester_name or requester_username} wants to follow you", html, text
    )


def build_follow_accepted(*, owner_name: str, owner_username: str) -> EmailContent:
    """Your request to follow a private account was approved."""
    body = (
        _actor_tile(owner_name, owner_username, "Request approved")
        + f"<p><strong>{esc(owner_name or owner_username)}</strong> accepted your "
        "follow request — their collection is open to you now.</p>"
        + callout(
            "You'll see their collection in your community feed from here on.",
            tone="mint",
        )
    )
    html, text = render_email(
        "Your follow request was accepted.",
        body,
        ("See their collection", _profile_url(owner_username)),
        preheader=f"{owner_name or owner_username} accepted your follow request.",
        eyebrow="Community",
    )
    return EmailContent(
        f"{owner_name or owner_username} accepted your follow request", html, text
    )


def build_profile_likes(
    *,
    unsub_url: str,
    total_likes: int,
    recent_names: list[str] | None = None,
    period_label: str = "this week",
) -> EmailContent:
    """A *digest* of profile likes, never one email per like.

    Likes are the highest-volume social event by an order of magnitude, and
    one email each is the fastest way to teach someone to mute you. This is
    also the only social template that carries an unsubscribe footer, because
    a periodic summary is recurring mail rather than a transactional event.
    """
    from app.services.email_templates.announcements import unsubscribe_footer
    from app.services.email_templates.base import big_quote

    names = recent_names or []
    who = ""
    if names:
        shown = ", ".join(esc(n) for n in names[:3])
        others = total_likes - min(len(names), 3)
        who = (
            f"<p>From {shown}"
            + (f" and {others} other{'s' if others != 1 else ''}" if others > 0 else "")
            + ".</p>"
        )
    body = (
        big_quote(f"{total_likes}", f"profile likes {esc(period_label)}")
        + f'<p style="margin:0 0 8px;text-align:center;">'
        f"{chip('Community', tone='mint')}</p>"
        + who
        + callout(
            "People are finding your collection. Keep it up to date and it'll "
            "keep showing up.",
            tone="mint",
        )
    )
    html, text = render_email(
        "Your profile is getting attention.",
        body,
        ("View your profile", f"{app_url()}/app/community"),
        preheader=f"{total_likes} people liked your profile {period_label}.",
        eyebrow="Community",
        footer_html=unsubscribe_footer(unsub_url),
    )
    plural = "s" if total_likes != 1 else ""
    return EmailContent(
        f"{total_likes} profile like{plural} {period_label}", html, text
    )


__all__ = [
    "build_follow_accepted",
    "build_follow_request",
    "build_new_follower",
    "build_profile_likes",
]
