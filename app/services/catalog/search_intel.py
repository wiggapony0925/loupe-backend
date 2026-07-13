"""Deterministic query understanding for the marketplace search — zero AI.

Google-style: free text like ``"most recent charizard from evolving skies
under $50"`` is parsed into structured intent (sort, price band, rarity, game,
set, year) with plain rules — regexes and cached catalog lookups, no model
call, no latency, no cost — and the residual text (``"charizard"``) is what
actually hits the card search. The router merges this intent under any
explicit filter params (explicit always wins) and echoes what it understood
back to the client (``interpreted.chips``) so the UI can show it Google's
"showing results for" way.

Everything here is pure and synchronous except :func:`resolve_set`, which
matches a parsed set phrase against the (L2-cached) real set catalogs.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import BaseModel, Field

from app.utils.logger import get_logger

logger = get_logger("services.search_intel")

# ── Intent model ──────────────────────────────────────────────────────────


class QueryIntent(BaseModel):
    """What a free-text query *means*, parsed deterministically."""

    #: Residual card-name text after every modifier was consumed.
    text: str = ""
    game: str | None = None
    #: newest | oldest | price_asc | price_desc | name
    sort: str | None = None
    price_min: float | None = None
    price_max: float | None = None
    #: Case-insensitive regex over the card's rarity string.
    rarity_pattern: str | None = None
    #: Raw set phrase, e.g. "evolving skies" (resolved separately).
    set_query: str | None = None
    year: int | None = None
    #: Human-readable "understood" labels, in parse order.
    chips: list[str] = Field(default_factory=list)

    @property
    def plain(self) -> bool:
        """True when the query carries no modifiers beyond text + game —
        i.e. the fast true-pagination search path stays valid."""
        return (
            self.sort is None
            and self.price_min is None
            and self.price_max is None
            and self.rarity_pattern is None
            and self.set_query is None
            and self.year is None
        )

    @property
    def has_signal(self) -> bool:
        """True when parsing changed anything (worth echoing to the client)."""
        return bool(self.chips)


# ── Vocabulary ────────────────────────────────────────────────────────────

_GAME_ALIASES: tuple[tuple[str, str, str], ...] = (
    # (regex, game key, chip label) — longest/most-specific first.
    (r"magic[\s:]+the[\s:]+gathering", "magic", "Magic"),
    (r"pokemons?|pkmn", "pokemon", "Pokémon"),
    (r"yu[\s-]?gi[\s-]?oh!?|ygo", "yugioh", "Yu-Gi-Oh!"),
    (r"one[\s-]?piece", "onepiece", "One Piece"),
    (r"digimon", "digimon", "Digimon"),
    (r"magic|mtg", "magic", "Magic"),
)

_SORT_PHRASES: tuple[tuple[str, str, str], ...] = (
    # (regex, sort key, chip label) — longest first so "most recent" wins
    # before any shorter overlap.
    (r"most\s+recent(?:ly)?(?:\s+released)?", "newest", "Newest first"),
    (r"just\s+released|newly\s+released|new\s+arrivals?", "newest", "Newest first"),
    (r"newest|latest|recent", "newest", "Newest first"),
    (r"oldest|earliest", "oldest", "Oldest first"),
    (
        r"cheapest|lowest\s+price[ds]?|low\s+to\s+high|budget|affordable|cheap",
        "price_asc",
        "Cheapest first",
    ),
    (
        r"most\s+expensive|most\s+valuable|highest\s+price[ds]?|high\s+to\s+low|priciest|top\s+value",
        "price_desc",
        "Highest price first",
    ),
    (r"alphabetical(?:ly)?|a\s+to\s+z", "name", "A → Z"),
)

_RARITY_PHRASES: tuple[tuple[str, str, str], ...] = (
    # (regex, rarity regex, chip label). Deliberately conservative: bare
    # "rare"/"ex"/"v" stay in the text — they appear inside real card names
    # ("Rare Candy", "Charizard ex") and stripping them would break those
    # searches. Multi-word / unambiguous rarity vocabulary only.
    (r"secret\s+rares?", "secret", "Secret rare"),
    (r"rainbow\s+rares?|rainbows?", "rainbow", "Rainbow rare"),
    (r"illustration\s+rares?", "illustration", "Illustration rare"),
    (r"full\s+arts?", "full art|illustration", "Full art"),
    (r"ultra\s+rares?", "ultra", "Ultra rare"),
    (r"holographics?|holos?|foils?", "holo|foil", "Holo / foil"),
    (r"promos?", "promo", "Promo"),
)

#: Filler that never helps a card-name search once intent is extracted.
_NOISE = re.compile(
    r"\b(?:cards?|singles?|tcg|show\s+me|find(?:\s+me)?|search(?:\s+for)?|the)\b",
    re.IGNORECASE,
)

_PRICE = r"\$?(\d+(?:\.\d{1,2})?)"


def _fold(s: str) -> str:
    """Lowercase + strip diacritics ("Pokémon" → "pokemon")."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _dollars(v: float) -> str:
    return f"${v:g}"


# ── Parser ────────────────────────────────────────────────────────────────


def parse_query(q: str) -> QueryIntent:
    """Parse free text into :class:`QueryIntent`. Pure and deterministic.

    Matched modifiers are consumed out of the text; whatever remains is the
    card-name search term. A query with no recognized modifiers round-trips
    untouched (``plain`` stays True), so normal searches are unaffected.
    """
    intent = QueryIntent()
    text = _fold(q).strip()
    if not text:
        return intent

    def consume(pattern: str) -> re.Match[str] | None:
        nonlocal text
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            text = (text[: m.start()] + " " + text[m.end() :]).strip()
        return m

    # 1. Price bands (before sorts: "lowest priced under $50" etc.).
    m = consume(rf"\bbetween\s+{_PRICE}\s+and\s+{_PRICE}\b") or consume(
        rf"\$(\d+(?:\.\d{{1,2}})?)\s*(?:-|–|to)\s*{_PRICE}\b"
    )
    if m:
        intent.price_min = float(m.group(1))
        intent.price_max = float(m.group(2))
        intent.chips.append(
            f"{_dollars(intent.price_min)}–{_dollars(intent.price_max)}"
        )
    m = consume(rf"\b(?:under|below|less\s+than|up\s+to|at\s+most|max)\s+{_PRICE}\b")
    if m:
        intent.price_max = float(m.group(1))
        intent.chips.append(f"Under {_dollars(intent.price_max)}")
    m = consume(rf"\b(?:over|above|more\s+than|at\s+least|min)\s+{_PRICE}\b")
    if m:
        intent.price_min = float(m.group(1))
        intent.chips.append(f"Over {_dollars(intent.price_min)}")

    # 2. Sort phrases.
    for pattern, sort_key, chip in _SORT_PHRASES:
        if intent.sort is None and consume(rf"\b(?:{pattern})\b"):
            intent.sort = sort_key
            intent.chips.append(chip)

    # 3. Game.
    for pattern, game_key, chip in _GAME_ALIASES:
        if intent.game is None and consume(rf"\b(?:{pattern})\b"):
            intent.game = game_key
            intent.chips.append(chip)

    # 4. Rarity vocabulary.
    for pattern, rarity_rx, chip in _RARITY_PHRASES:
        if intent.rarity_pattern is None and consume(rf"\b(?:{pattern})\b"):
            intent.rarity_pattern = rarity_rx
            intent.chips.append(chip)

    # 5. Year (before the set phrase so "from 2021" is a year, not a set).
    m = consume(r"\b(19[89]\d|20[0-3]\d)\b")
    if m:
        intent.year = int(m.group(1))
        intent.chips.append(f"Year {intent.year}")

    # 6. Set phrase — a trailing "from/in <the> <phrase> <set>" or "<phrase> set".
    m = consume(r"\b(?:from|in)\s+(?:the\s+)?(.+?)(?:\s+set)?\s*$")
    if m is None:
        m = consume(r"\b(\S.+?)\s+set\s*$")
    if m:
        phrase = m.group(1).strip(" .,!?")
        if len(phrase) >= 3:
            intent.set_query = phrase
            intent.chips.append(f"Set: {phrase.title()}")

    # 7. Residual card-name text.
    text = _NOISE.sub(" ", text)
    intent.text = re.sub(r"\s{2,}", " ", text).strip(" .,!?-–")
    return intent


# ── Set resolution — parsed phrase → a real set from the cached catalogs ──

#: Games whose set lists carry dates + browse supports set scoping.
_SET_GAMES: tuple[str, ...] = ("pokemon", "magic", "yugioh")


def _norm_set(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", _fold(s)).strip()


async def resolve_set(set_query: str, game: str | None = None) -> dict[str, Any] | None:
    """Match a parsed set phrase against the real set catalogs (L2-cached).

    Ranked match: exact normalized name → prefix → contains → all-tokens.
    Ties go to the newest release, which is what a shopper means by an
    ambiguous phrase ("base" → the most recent "…base…" set). Returns the
    set dict (``id``/``name``/``tcg``/``release_date``) or ``None``.
    """
    from app.services.catalog import card_search_service

    target = _norm_set(set_query)
    if not target:
        return None
    games = [game] if game in _SET_GAMES else list(_SET_GAMES)

    best: tuple[int, str, dict[str, Any]] | None = None  # (rank, date, set)
    for g in games:
        try:
            body = await card_search_service.list_sets(g)
        except Exception as exc:  # pragma: no cover - upstream best effort
            logger.debug("resolve_set list_sets(%s) failed: %s", g, exc)
            continue
        for item in body.get("results") or []:
            name = _norm_set(str(item.get("name") or ""))
            if not name:
                continue
            if name == target:
                rank = 0
            elif name.startswith(target):
                rank = 1
            elif target in name:
                rank = 2
            elif all(tok in name.split() for tok in target.split()):
                rank = 3
            else:
                continue
            key = (rank, str(item.get("release_date") or ""))
            if (
                best is None
                or key[0] < best[0]
                or (key[0] == best[0] and key[1] > best[1])
            ):
                best = (key[0], key[1], item)
    return best[2] if best else None


# ── Pool filtering/sorting helpers (shared by the router paths) ──────────


def _price_of(card: dict[str, Any]) -> float | None:
    pricing = card.get("pricing_summary") or {}
    for key in ("market", "high", "mid", "low"):
        chosen = pricing.get(key)
        if isinstance(chosen, dict) and chosen.get("amount") is not None:
            try:
                return float(chosen["amount"])
            except (TypeError, ValueError):
                continue
    return None


def filter_cards(
    cards: list[dict[str, Any]],
    intent: QueryIntent,
    set_name: str | None = None,
) -> list[dict[str, Any]]:
    """Apply the parsed filters (price band / rarity / year / set) to a pool."""
    out = cards
    if intent.price_min is not None or intent.price_max is not None:
        kept = []
        for c in out:
            price = _price_of(c)
            if price is None:
                continue
            if intent.price_min is not None and price < intent.price_min:
                continue
            if intent.price_max is not None and price > intent.price_max:
                continue
            kept.append(c)
        out = kept
    if intent.rarity_pattern:
        try:
            rx = re.compile(intent.rarity_pattern, re.IGNORECASE)
            out = [c for c in out if c.get("rarity") and rx.search(str(c["rarity"]))]
        except re.error:  # pragma: no cover - patterns are our own vocabulary
            pass
    if intent.year is not None:
        out = [c for c in out if c.get("year") == intent.year]
    if set_name:
        want = _norm_set(set_name)
        out = [
            c for c in out if want and want in _norm_set(str(c.get("set_name") or ""))
        ]
    return out


def sort_cards(cards: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """The pooled-path sort, extended with the parsed newest/oldest orders."""
    if sort == "newest":
        return sorted(
            cards, key=lambda c: (c.get("year") is None, -(c.get("year") or 0))
        )
    if sort == "oldest":
        return sorted(cards, key=lambda c: (c.get("year") is None, c.get("year") or 0))
    if sort == "price_asc":
        return sorted(cards, key=lambda c: (_price_of(c) is None, _price_of(c) or 0.0))
    if sort == "price_desc":
        return sorted(cards, key=lambda c: _price_of(c) or 0.0, reverse=True)
    if sort == "name":
        return sorted(cards, key=lambda c: (c.get("name") or "").lower())
    return cards


def interpreted_payload(
    intent: QueryIntent, resolved_set: dict[str, Any] | None
) -> dict[str, Any]:
    """The ``interpreted`` block echoed to clients — what the parser understood."""
    chips = list(intent.chips)
    if resolved_set is not None and intent.set_query:
        # Upgrade the raw phrase chip to the resolved set's real name.
        chips = [
            f"Set: {resolved_set.get('name')}" if c.startswith("Set: ") else c
            for c in chips
        ]
    return {
        "text": intent.text,
        "game": intent.game,
        "sort": intent.sort,
        "price_min": intent.price_min,
        "price_max": intent.price_max,
        "rarity": intent.rarity_pattern,
        "set": (resolved_set or {}).get("name") or intent.set_query,
        "set_id": (resolved_set or {}).get("id"),
        "year": intent.year,
        "chips": chips,
    }


__all__ = [
    "QueryIntent",
    "filter_cards",
    "interpreted_payload",
    "parse_query",
    "resolve_set",
    "sort_cards",
]
