"""Parse OCR output into structured card-identifying fields.

The TCG OCR problem is dominated by *noise above the title*: copyright
text, set logos, energy symbols, mana cost glyphs, attack damage
numbers, illustrator credits, foot-of-card legalese. Sending the raw
``full_text`` straight to a catalog text search wastes most of the
upstream's matching power on tokens the user doesn't care about.

This parser produces a :class:`ParsedCard` with a single best-guess
``title`` plus optional ``set_code``, ``card_number``, ``year``, ``hp``,
``atk_def``, and ``mana_cost`` fields. Everything is best-effort and
returns ``None`` rather than raising — the ranking layer treats missing
fields as "no signal" rather than disqualifying a candidate.

Design constraint: this module is pure-Python with no upstream API
calls so it can be unit-tested with synthetic OCR strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ──────────────────────────────────────────────────────────────────────
# Regexes used during parsing. Compiled once at import time.
# ──────────────────────────────────────────────────────────────────────

# Pokémon HP marker: "HP 120" or "120 HP". Captures the number.
_HP_RE = re.compile(r"\b(?:HP\s*)?(\d{1,3})\s*HP\b|\bHP\s*(\d{1,3})\b", re.IGNORECASE)
# Yu-Gi-Oh ATK/DEF footer: "ATK/2500 DEF/2100" or "ATK 2500 DEF 2100".
_ATK_DEF_RE = re.compile(
    r"ATK[\s/:]*(\d{1,4}).{0,12}DEF[\s/:]*(\d{1,4})", re.IGNORECASE | re.DOTALL
)
# MTG mana glyph leak (Vision sometimes catches "{R}", "{2}{W}{B}", etc.).
_MANA_RE = re.compile(r"\{[0-9WUBRGCXS/]+\}")
# One Piece DON!! marker for card type/cost line.
_OP_DON_RE = re.compile(r"DON!{1,3}", re.IGNORECASE)
# A standard Pokémon / MTG / OP set+number footer like "123/198" or "SV1 045/198".
_NUMBERING_RE = re.compile(r"\b(\d{1,3})\s*/\s*(\d{1,3})\b")
# 4-digit year (1996-2099) - copyright or release stamp.
_YEAR_RE = re.compile(r"\b(199[6-9]|20\d{2})\b")
# Set code candidates: 2-6 uppercase letters/digits with at least one letter,
# typically printed next to the number ("SV1", "BLB", "MOM", "LOB", "BS").
_SET_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,5})\b")
# Lines we *never* want to consider as the title. Lowercased contains-check.
_TITLE_BLACKLIST_TOKENS = (
    "©",
    "(c)",
    "illus.",
    "illustration",
    "illustrated",
    "trademark",
    "all rights reserved",
    "nintendo",
    "creatures inc",
    "game freak",
    "konami",
    "wizards of the coast",
    "tcgplayer",
    "pokemon",  # rarely a real title token, often the logo
    "evolution",  # generic line under names
    "basic",
    "stage 1",
    "stage 2",
    "ex",  # appears below title too often as a standalone token
    "weakness",
    "resistance",
    "retreat",
    "ability",
    "this attack",
    "your opponent",
    "discard",
    "shuffle",
    "draw",
    "search",
    "your deck",
)


@dataclass(frozen=True, slots=True)
class ParsedCard:
    """Structured fields extracted from raw OCR text."""

    title: str | None = None
    # Multiple title candidates (with confidence) so the ranker can try
    # the runner-up when the top guess doesn't match anything.
    title_candidates: list[tuple[str, float]] = field(default_factory=list)
    set_code: str | None = None
    card_number: str | None = None  # "123/198" — kept verbatim
    year: int | None = None
    hp: int | None = None
    atk_def: tuple[int, int] | None = None
    mana_cost: list[str] | None = None
    tcg_hints: list[str] = field(default_factory=list)  # ("pokemon", "magic", ...)
    # All non-empty lines after cleanup. Useful for fallback "search the
    # whole card" fuzzy match.
    cleaned_lines: list[str] = field(default_factory=list)


# ───────────────────────────────────────────────────────────────── helpers


def _looks_like_title(line: str) -> bool:
    """Heuristic: is this line plausibly the card name?

    Filters out short OCR artifacts ("E", "®"), obvious legalese, and
    lines that are dominated by digits / punctuation.
    """
    stripped = line.strip()
    if len(stripped) < 2:
        return False
    lower = stripped.lower()
    for token in _TITLE_BLACKLIST_TOKENS:
        if token in lower:
            return False
    # Reject lines that are mostly numbers / symbols.
    alpha = sum(1 for c in stripped if c.isalpha())
    if alpha / max(1, len(stripped)) < 0.5:
        return False
    # Reject super-long lines (rules text, not titles).
    return len(stripped) <= 50


def _clean_line(line: str) -> str:
    # Collapse whitespace, trim, remove leading energy/cost glyphs that
    # bleed onto the title line (e.g. "⚡⚡ Pikachu" → "Pikachu").
    line = line.replace("\u2022", " ")  # bullet
    line = re.sub(r"^[\W_]+", "", line)
    line = re.sub(r"[\W_]+$", "", line)
    line = re.sub(r"\s+", " ", line)
    return line.strip()


def _title_score(line: str, idx: int, total_lines: int) -> float:
    """Score how likely a line is the card title.

    Higher = better. Top-of-card lines win ties; mixed-case names beat
    SHOUTING; lines with a digit get a small penalty.
    """
    score = 1.0
    # Position prior: titles are almost always in the top 35% of the card.
    if total_lines > 0:
        pos = idx / max(1, total_lines - 1)
        score += max(0.0, 1.0 - pos) * 0.6
    # Mixed case is more name-like than ALL CAPS.
    if not line.isupper():
        score += 0.15
    # Penalize lines with digits (those are usually HP / damage / set numbers).
    if any(c.isdigit() for c in line):
        score -= 0.3
    # Penalize all-uppercase short tokens (set logos like "SV1").
    if line.isupper() and len(line) <= 4:
        score -= 0.5
    return score


# ───────────────────────────────────────────────────────────────── parser


def parse_ocr_text(raw_text: str) -> ParsedCard:
    """Return a :class:`ParsedCard` populated from ``raw_text``.

    Safe to call with an empty string — returns an empty ParsedCard
    rather than raising. Title detection ranks candidate lines using
    :func:`_title_score` and reports the top 3 so the ranker can try
    fallbacks.
    """
    if not raw_text:
        return ParsedCard()

    lines = [_clean_line(line) for line in raw_text.splitlines()]
    cleaned = [line for line in lines if line]

    # ── HP / ATK / DEF / mana / year — read from the full text ──────────
    hp: int | None = None
    hp_match = _HP_RE.search(raw_text)
    if hp_match:
        hp_str = hp_match.group(1) or hp_match.group(2)
        if hp_str and hp_str.isdigit():
            v = int(hp_str)
            if 10 <= v <= 999:
                hp = v

    atk_def: tuple[int, int] | None = None
    ad_match = _ATK_DEF_RE.search(raw_text)
    if ad_match:
        a, d = int(ad_match.group(1)), int(ad_match.group(2))
        if 0 <= a <= 9999 and 0 <= d <= 9999:
            atk_def = (a, d)

    mana = _MANA_RE.findall(raw_text)
    mana_cost = mana if mana else None

    year = None
    y_match = _YEAR_RE.search(raw_text)
    if y_match:
        year = int(y_match.group(1))

    # ── Numbering + set code (usually on the same footer line) ─────────
    card_number = None
    set_code = None
    num_match = _NUMBERING_RE.search(raw_text)
    if num_match:
        card_number = f"{num_match.group(1)}/{num_match.group(2)}"
        # Look at the same line for an adjacent set code.
        for line in cleaned:
            if num_match.group(0) in line:
                for tok in line.split():
                    if tok == num_match.group(0):
                        continue
                    candidate = tok.strip(".,()[]:/-").upper()
                    if _SET_CODE_RE.fullmatch(candidate) and not candidate.isdigit():
                        set_code = candidate
                        break
                break

    # ── TCG hints ───────────────────────────────────────────────────────
    tcg_hints: list[str] = []
    if hp is not None:
        tcg_hints.append("pokemon")
    if atk_def is not None:
        tcg_hints.append("yugioh")
    if mana_cost:
        tcg_hints.append("magic")
    if _OP_DON_RE.search(raw_text):
        tcg_hints.append("onepiece")

    # ── Title candidates ────────────────────────────────────────────────
    scored: list[tuple[str, float]] = []
    total = len(cleaned)
    for idx, line in enumerate(cleaned):
        if not _looks_like_title(line):
            continue
        scored.append((line, _title_score(line, idx, total)))
    # Normalize scores to [0,1] for downstream consumers.
    if scored:
        top = max(s for _, s in scored)
        bottom = min(s for _, s in scored)
        span = max(0.01, top - bottom)
        scored = [
            (line, round(0.5 + 0.5 * (s - bottom) / span, 4)) for line, s in scored
        ]
        scored.sort(key=lambda t: t[1], reverse=True)
    title_candidates = scored[:3]
    title = title_candidates[0][0] if title_candidates else None

    return ParsedCard(
        title=title,
        title_candidates=title_candidates,
        set_code=set_code,
        card_number=card_number,
        year=year,
        hp=hp,
        atk_def=atk_def,
        mana_cost=mana_cost,
        tcg_hints=tcg_hints,
        cleaned_lines=cleaned,
    )


__all__ = ["ParsedCard", "parse_ocr_text"]
