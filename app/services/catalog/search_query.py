"""Smart catalog search query parsing — shared by mirror + live providers.

Understands collector numbers (``001/34``, ``58/102``), mixed name+number
queries (``charizard 4/102``), and normalizes text for fuzzy ranking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# NNN/NNN collector fractions embedded in free text.
_COLLECTOR_NUMBER_RE = re.compile(
    r"(?<!\d)(?P<left>0*\d{1,4})\s*/\s*(?P<right>0*\d{1,4})(?!\d)"
)

_SAFE_RE = re.compile(r"[^A-Za-z0-9\-/ ]+")


def bare_number(number: str | None) -> str | None:
    """``58/102`` → ``58``; ``008`` → ``8``."""
    if not number:
        return None
    left = str(number).split("/", 1)[0].strip().lstrip("0")
    return left if left.isdigit() else None


@dataclass(frozen=True, slots=True)
class ParsedSearchQuery:
    raw: str
    name_text: str
    number_raw: str | None
    number_bare: str | None
    set_total: str | None
    name_tokens: tuple[str, ...]

    @property
    def has_number(self) -> bool:
        return self.number_bare is not None

    @property
    def rank_text(self) -> str:
        """Text passed to the relevance ranker."""
        if self.name_text.strip():
            return self.name_text.strip()
        if self.number_raw:
            return self.number_raw
        return self.raw


def parse_search_query(q: str) -> ParsedSearchQuery:
    raw = (q or "").strip()
    name_text = raw
    number_raw: str | None = None
    number_bare: str | None = None
    set_total: str | None = None

    match = _COLLECTOR_NUMBER_RE.search(raw)
    if match:
        left = match.group("left")
        right = match.group("right")
        number_raw = f"{left}/{right}"
        number_bare = bare_number(number_raw)
        set_total = right.lstrip("0") or right
        name_text = f"{raw[: match.start()]} {raw[match.end() :]}".strip()

    # Whole query is a bare collector index — ``58``, ``001``.
    if number_bare is None:
        compact = raw.replace(" ", "")
        if compact.isdigit() and 1 <= len(compact) <= 4:
            number_bare = compact.lstrip("0") or compact
            number_raw = compact
            name_text = ""

    cleaned = _SAFE_RE.sub(" ", name_text).strip().lower()
    tokens = [t for t in cleaned.split() if t]

    # Trailing bare index — ``lightning bolt 161``, ``charizard 4``.
    if tokens and tokens[-1].isdigit() and len(tokens[-1]) <= 4:
        trailing = tokens.pop()
        if number_bare is None:
            number_bare = trailing.lstrip("0") or trailing
            number_raw = number_raw or trailing

    return ParsedSearchQuery(
        raw=raw,
        name_text=" ".join(tokens) if tokens else name_text,
        number_raw=number_raw,
        number_bare=number_bare,
        set_total=set_total,
        name_tokens=tuple(tokens),
    )


def pokemon_lucene_query(parsed: ParsedSearchQuery) -> str:
    """Build a Pokémon TCG API Lucene query from a parsed query."""
    parts: list[str] = []
    for tok in parsed.name_tokens:
        parts.append(f"name:{tok}*")
    if parsed.number_bare:
        parts.append(f"number:{parsed.number_bare}")
    if parts:
        return " ".join(parts)
    cleaned = _SAFE_RE.sub(" ", parsed.raw).strip().lower()
    tokens = [t for t in cleaned.split() if t]
    if tokens:
        return " ".join(f"name:{tok}*" for tok in tokens)
    return f'name:"{parsed.raw}"'


def pokemon_relaxed_query(parsed: ParsedSearchQuery) -> str | None:
    tokens = [t for t in parsed.name_tokens if len(t) >= 2]
    if not tokens:
        return None
    parts = [f"name:*{tok}*" for tok in tokens]
    if parsed.number_bare:
        parts.append(f"number:{parsed.number_bare}")
    return " OR ".join(parts)


def scryfall_query(parsed: ParsedSearchQuery) -> str:
    parts: list[str] = []
    if parsed.name_tokens:
        parts.append(" ".join(parsed.name_tokens))
    if parsed.number_bare:
        parts.append(f"cn:{parsed.number_bare}")
    return " ".join(parts) if parts else parsed.raw


__all__ = [
    "ParsedSearchQuery",
    "bare_number",
    "parse_search_query",
    "pokemon_lucene_query",
    "pokemon_relaxed_query",
    "scryfall_query",
]
