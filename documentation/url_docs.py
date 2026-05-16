"""Upstream URLs referenced by the backend — rendered into the OpenAPI doc."""

from __future__ import annotations

from app.config import get_settings

#: Logical name → settings attribute holding the URL.
UPSTREAM_URLS: dict[str, str] = {
    "Pokémon TCG IO": "pokemon_tcg_base_url",
    "Scryfall (Magic)": "scryfall_base_url",
    "YGOPRODeck (Yu-Gi-Oh)": "ygoprodeck_base_url",
    "Apple JWKS": "apple_jwks_url",
    "Google JWKS": "google_jwks_url",
}


def render_upstream_urls() -> str:
    """Return a markdown bullet list of upstream URLs the backend talks to."""
    s = get_settings()
    lines = ["| Service | URL |", "| --- | --- |"]
    for label, attr in UPSTREAM_URLS.items():
        lines.append(f"| {label} | `{getattr(s, attr, '(unset)')}` |")
    return "\n".join(lines)


__all__ = ["UPSTREAM_URLS", "render_upstream_urls"]
