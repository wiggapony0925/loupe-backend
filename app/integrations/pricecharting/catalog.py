"""PriceCharting as a Pokémon *catalog* source (not just pricing).

Our card catalog comes from pokemontcg.io, which is English-only — so Japanese
(and other non-English) printings never appear in search. PriceCharting, our
main price source, *does* carry them: it encodes the language in the set /
console name (``"Pokemon Japanese …"``). This module fetches + parses those
products so the catalog service can surface them, language-tagged.

Works on the current tier via the live ``/api/products`` (search) and
``/api/product`` (detail) endpoints; the same rows also arrive through the
Legendary CSV mirror. Pure fetch + parse — the catalog service owns the
``UnifiedCard`` conversion and caching.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from app.integrations.pricecharting.provider import BASE_URL, token
from app.utils.logger import get_logger

logger = get_logger("integrations.pricecharting.catalog")

_TIMEOUT = 10.0

# Console-name keyword → ISO language. PriceCharting names non-English Pokémon
# sets "Pokemon Japanese …" etc.; anything unmarked is English.
_LANG_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("japanese", "ja"),
    ("chinese", "zh"),
    ("korean", "ko"),
    ("german", "de"),
    ("french", "fr"),
    ("italian", "it"),
    ("spanish", "es"),
)

_NUM_RE = re.compile(r"#\s*([A-Za-z0-9]+(?:/[A-Za-z0-9]+)?)")
_POKEMON_PREFIX_RE = re.compile(r"^pokemon\s+", re.IGNORECASE)


def configured() -> bool:
    """A usable PriceCharting token is set (so the catalog augment can run)."""
    return token() is not None


def language_from_console(console: str | None) -> str:
    c = (console or "").lower()
    for keyword, iso in _LANG_KEYWORDS:
        if keyword in c:
            return iso
    return "en"


def is_pokemon_console(console: str | None) -> bool:
    return (console or "").lower().startswith("pokemon")


def set_name_from_console(console: str | None) -> str:
    """``"Pokemon Japanese Base Set"`` → ``"Japanese Base Set"`` (keeps the
    language word so the row visibly reads as Japanese)."""
    return _POKEMON_PREFIX_RE.sub("", console or "").strip() or (console or "")


def split_name_number(product_name: str | None) -> tuple[str, str | None]:
    """``"Charizard #4"`` → ``("Charizard", "4")``."""
    if not product_name:
        return "", None
    match = _NUM_RE.search(product_name)
    number = match.group(1) if match else None
    name = _NUM_RE.sub("", product_name).strip()
    return (name or product_name), number


def parse_product(product: dict[str, Any]) -> dict[str, Any] | None:
    """A raw PriceCharting product → a normalised Pokémon catalog entry, or
    ``None`` if it isn't a Pokémon product."""
    pc_id = product.get("id")
    name_raw = product.get("product-name")
    console = product.get("console-name")
    if not pc_id or not name_raw or not is_pokemon_console(console):
        return None
    name, number = split_name_number(str(name_raw))
    return {
        "id": f"pricecharting:{pc_id}",
        "pc_id": str(pc_id),
        "name": name,
        "number": number,
        "console": console,
        "set_name": set_name_from_console(console),
        "language": language_from_console(console),
    }


async def _get(path: str, params: dict[str, str]) -> dict[str, Any] | None:
    tok = token()
    if not tok:
        return None
    query = "&".join(f"{k}={quote(str(v))}" for k, v in {"t": tok, **params}.items())
    url = f"{BASE_URL}/{path}?{query}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:  # never let a catalog augment break search
        logger.debug("pricecharting catalog %s failed: %s", path, exc)
        return None


async def search_products(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Live ``/api/products`` search — raw product rows (id/name/console)."""
    if not query.strip():
        return []
    data = await _get("products", {"q": query})
    products = (data or {}).get("products") or []
    return products[:limit] if isinstance(products, list) else []


async def get_product(pc_id: str) -> dict[str, Any] | None:
    """Live ``/api/product`` by id — full row incl. the price ladder."""
    return await _get("product", {"id": pc_id})


__all__ = [
    "configured",
    "get_product",
    "is_pokemon_console",
    "language_from_console",
    "parse_product",
    "search_products",
    "set_name_from_console",
    "split_name_number",
]
