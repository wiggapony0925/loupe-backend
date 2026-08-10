"""Per-platform social links: the allowlist and the normaliser.

Everything stored in ``SocialProfile.links`` passes through here first, so
the column only ever holds canonical ``https`` URLs keyed by a platform we
recognise. Clients get to render a row of icons without ever parsing,
guessing, or sanitising — and a stored value is safe to drop straight into
an ``href``, which is exactly why ``javascript:``/``data:`` schemes are
refused at the door rather than filtered at render time.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException

#: Every platform a profile may link. A closed list on purpose: an open
#: key space would turn the column into arbitrary user JSON, and clients
#: (which map keys to icons) would silently drop anything they don't know —
#: better to tell the user at write time than to eat their link.
PLATFORMS = frozenset(
    {
        "instagram",
        "x",
        "facebook",
        "tiktok",
        "twitch",
        "web",
        "youtube",
        "whatnot",
        "drip",
        "ebay",
        "cardmarket",
        "tcgplayer",
        "kick",
    }
)

#: Platforms where people think in handles, not URLs — "@lisacollects", not
#: "https://instagram.com/lisacollects". A bare handle on one of these is
#: expanded to the platform's canonical profile URL so the client never has
#: to know each site's URL shape.
HANDLE_URLS = {
    "instagram": "https://instagram.com/{}",
    "x": "https://x.com/{}",
    "tiktok": "https://tiktok.com/@{}",
    "youtube": "https://youtube.com/@{}",
    "twitch": "https://twitch.tv/{}",
    "kick": "https://kick.com/{}",
}

#: Conservative union of what the handle platforms accept. Anything odder
#: is almost certainly a paste error (or an injection attempt), and a 422
#: now beats a dead link on a public profile.
HANDLE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
)
MAX_HANDLE_CHARS = 64

#: Cap per stored URL. Real profile links are short; anything longer is
#: tracking-parameter soup or abuse of the column as blob storage.
MAX_LINK_CHARS = 300

#: The only schemes a profile link may carry once stored. An allowlist, not
#: a blocklist: ``javascript:``, ``data:``, ``vbscript:`` and whatever gets
#: invented next all fail the same check.
ALLOWED_SCHEMES = frozenset({"http", "https"})


def _is_valid_handle(handle: str) -> bool:
    return 0 < len(handle) <= MAX_HANDLE_CHARS and all(
        ch in HANDLE_CHARS for ch in handle
    )


def normalize_links(raw: dict[str, str] | None) -> dict[str, str] | None:
    """Canonicalise a client's links dict, or 422 on anything off-menu.

    Empty/whitespace values are dropped rather than rejected — sending
    ``""`` is how a client removes one entry without resending the rest of
    its state. Returns ``None`` when nothing survives, so the column never
    stores an empty ``{}`` that reads differently from "no links".
    """
    if raw is None:
        return None

    out: dict[str, str] = {}
    for key, raw_value in raw.items():
        if key not in PLATFORMS:
            raise HTTPException(status_code=422, detail=f"Unknown link platform: {key}")
        value = (raw_value or "").strip()
        if not value:
            continue

        # A bare handle ("@lisacollects" / "lisacollects") on a handle
        # platform expands to the canonical profile URL. Detection is
        # "no dot, no colon": a dot means they pasted a domain, a colon
        # means they pasted (or crafted) something with a scheme — both go
        # down the URL path below and face the scheme allowlist there.
        if key in HANDLE_URLS and "." not in value and ":" not in value:
            handle = value.lstrip("@")
            if not _is_valid_handle(handle):
                raise HTTPException(
                    status_code=422,
                    detail=f"That doesn't look like a valid {key} handle",
                )
            url = HANDLE_URLS[key].format(handle)
        else:
            # People paste "lisacollects.com", not "https://lisacollects.com".
            # Only a schemeless value with a dot gets the assist — helping
            # "javascript:alert(1)" or "lisacollects" into a URL would just
            # launder garbage past the checks below.
            url = value
            parsed = urlparse(url)
            if not parsed.scheme and "." in url:
                url = f"https://{url}"
                parsed = urlparse(url)
            if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
                raise HTTPException(
                    status_code=422,
                    detail=f"That doesn't look like a valid {key} link",
                )

        if len(url) > MAX_LINK_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"The {key} link is too long (max {MAX_LINK_CHARS} chars)",
            )
        out[key] = url

    return out or None


__all__ = ["PLATFORMS", "normalize_links"]
