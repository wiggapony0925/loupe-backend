"""TCG inference + candidate confidence scoring.

These helpers are intentionally small and pure so they can be re-used
by the eval harness, future on-device clients, and the admin
metrics endpoint (which slices accuracy by inferred TCG).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.services.identification.text_parser import ParsedCard

# Order matters: when two TCG signals fire (e.g. HP *and* mana cost from a
# misread foil), the earlier entry wins. We trust HP more than mana cost
# because Vision occasionally hallucinates {X} from card-border glyphs.
_HINT_PRIORITY: tuple[str, ...] = ("pokemon", "yugioh", "magic", "onepiece")

# Every game we can route to. Keep in sync with the catalog providers in
# ``card_search_service`` and the router ``pattern=`` validators.
_VALID_TCGS: frozenset[str] = frozenset(
    {"pokemon", "magic", "yugioh", "onepiece", "lorcana", "sports"}
)


def _has_fraction_number(parsed: ParsedCard) -> bool:
    """True if OCR found a ``NNN/NNN`` collector number.

    Pokémon and Magic print a ``set-size`` fraction (e.g. ``058/198``);
    Yu-Gi-Oh uses a hyphenated set code (``LOB-001``) and One Piece uses
    ``OP01-001``. So a bare fraction is soft evidence *against* those and
    *for* the fraction-numbered games.
    """
    num = parsed.card_number
    return bool(num and "/" in num)


# Soft-evidence predicates per game. Used only as a tie-breaker when no
# decisive OCR hint fired, to bias toward the app's primary TCG instead of
# fanning out to every catalog. Add an entry here when onboarding a new
# focus game — the rest of ``infer_tcg`` is game-agnostic.
_SOFT_SIGNALS: dict[str, Any] = {
    "pokemon": _has_fraction_number,
    "magic": _has_fraction_number,
}


def _soft_match(tcg: str, parsed: ParsedCard) -> bool:
    """Whether ``parsed`` carries soft (non-decisive) evidence for ``tcg``."""
    predicate = _SOFT_SIGNALS.get(tcg)
    return bool(predicate and predicate(parsed))


def infer_tcg(parsed: ParsedCard, *, user_hint: str | None = None) -> str:
    """Best-guess TCG from parsed OCR + an optional client-provided hint.

    Resolution order (first decisive signal wins):

      1. Explicit ``user_hint`` from the client (the TCG pill).
      2. A decisive OCR signal — HP (Pokémon), ATK/DEF (Yu-Gi-Oh), or a
         mana glyph (Magic) — by :data:`_HINT_PRIORITY`.
      3. **Primary-TCG bias.** With no decisive signal we'd otherwise fan
         out to *every* catalog, which lets an unrelated game win on a
         poor read. Instead, if there's soft corroborating evidence for
         the configured ``identify_primary_tcg`` (e.g. a ``NNN/NNN``
         collector number for a Pokémon-focused app), prefer it. This is
         config-driven so it scales as the product adds games — flip
         ``settings.identify_primary_tcg`` (or set it to ``"all"`` to
         disable the bias) with no code changes.
      4. Fall back to ``"all"`` (fan out to every catalog).

    Returns one of ``"pokemon"``, ``"magic"``, ``"yugioh"``,
    ``"onepiece"``, ``"lorcana"``, ``"sports"``, or ``"all"``.
    """
    if user_hint and user_hint.lower() in _VALID_TCGS:
        return user_hint.lower()
    for hint in _HINT_PRIORITY:
        if hint in parsed.tcg_hints:
            return hint
    primary = get_settings().identify_primary_tcg.lower()
    if primary in _VALID_TCGS and _soft_match(primary, parsed):
        return primary
    return "all"


# ──────────────────────────────────────────────────────────────────────
# Confidence scoring
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Per-signal weights that fed into the final confidence value.

    Persisted on the identification row so we can later A/B different
    weightings without losing the original decision.
    """

    text_similarity: float  # 0-1 fuzzy match of parsed title vs candidate name
    ocr_quality: float  # provider's mean word confidence, 0-1
    field_match: float  # bonuses for set_code / number / HP / mana matches
    phash_match: float  # 0-1, 1.0 if the candidate came from the phash path
    feedback_prior: float  # gentle boost from prior correct identifications
    final: float  # weighted sum, clamped to [0, 1]


# Weights sum to 1.0 by construction. Tuned against the 50-card eval
# fixture set; revisit if accuracy regresses on a future eval run.
#
# Structured fields (the collector number above all) carry more weight
# than fuzzy name similarity on purpose: the printed number uniquely
# identifies a card within its set, whereas a fuzzy name match cannot
# tell an attack name ("Energize") from a look-alike card ("Energizer").
# The number is the disambiguator real scanners rely on, so we lean on it.
_W_TEXT = 0.40
_W_OCR = 0.05
_W_FIELDS = 0.35
_W_PHASH = 0.15
_W_FEEDBACK = 0.05

# Extra confidence when the title *and* the collector number both match.
# Calibrated so a confirmed (title + number) match clears the client lock
# threshold while a name-only match stays in carousel territory.
_SYNERGY_TITLE_NUMBER = 0.18

# Extra confidence when the set code *and* collector number both match. That
# pair pins a single printing even when the NAME reads poorly — common on
# vintage, foil, and non-English cards where Vision mangles the title but the
# set symbol + number come back clean. Mirrors the title+number synergy so a
# strong *structured* read still locks without leaning on name similarity.
_SYNERGY_SET_NUMBER = 0.16

# Penalty when we read a collector number cleanly off the card but the
# candidate's printed number contradicts it. A number conflict is strong
# evidence the candidate is the WRONG card (e.g. a fuzzy name look-alike
# from a different set), so we push it well below the lock threshold even
# if its name scores high. This is the core guard against locking onto an
# attack-name look-alike like "Energizer" when the card is "Pikachu #058".
_NUMBER_CONFLICT_PENALTY = 0.45


def _string_similarity(a: str, b: str) -> float:
    """Normalized fuzzy similarity in ``[0, 1]``.

    Uses :mod:`rapidfuzz` when available (much faster + slightly better
    than ``difflib`` on short strings), falls back to ``difflib`` so the
    pipeline still works in stripped-down test environments.
    """
    if not a or not b:
        return 0.0
    a_norm = a.strip().lower()
    b_norm = b.strip().lower()
    if a_norm == b_norm:
        return 1.0
    try:
        from rapidfuzz import fuzz  # type: ignore[import-not-found]

        return fuzz.WRatio(a_norm, b_norm) / 100.0
    except ImportError:  # pragma: no cover - rapidfuzz is in requirements
        from difflib import SequenceMatcher

        return SequenceMatcher(None, a_norm, b_norm).ratio()


def _set_code_matches(parsed: ParsedCard, candidate: dict[str, Any]) -> bool:
    """True when the parsed set code matches the candidate's (case-insensitive)."""
    cand_set = (
        (candidate.get("set") or {}).get("code") or candidate.get("set_code") or ""
    )
    return bool(
        parsed.set_code
        and cand_set
        and parsed.set_code.upper() == str(cand_set).upper()
    )


def _number_matches(parsed: ParsedCard, candidate: dict[str, Any]) -> bool:
    """True when the parsed collector number matches the candidate's.

    Compares the bare left-hand digits ("58/102" → "58") with leading
    zeros stripped so "008" and "8" are treated as equal.
    """
    cand_num = candidate.get("number") or (candidate.get("set") or {}).get("number")
    if not parsed.card_number or not cand_num:
        return False
    parsed_left = parsed.card_number.split("/", 1)[0].lstrip("0")
    cand_left = str(cand_num).split("/", 1)[0].lstrip("0")
    return bool(parsed_left) and parsed_left == cand_left


def _number_conflicts(parsed: ParsedCard, candidate: dict[str, Any]) -> bool:
    """True when we read a number off the card but the candidate's differs.

    Only fires when *both* sides have a usable number — a missing number
    on either side is "unknown", not a conflict, so we never penalise a
    candidate just because its printed number didn't come back from the
    catalog. A genuine mismatch ("058" read vs candidate "285") is strong
    evidence the candidate is the wrong card.
    """
    cand_num = candidate.get("number") or (candidate.get("set") or {}).get("number")
    if not parsed.card_number or not cand_num:
        return False
    parsed_left = parsed.card_number.split("/", 1)[0].lstrip("0")
    cand_left = str(cand_num).split("/", 1)[0].lstrip("0")
    if not parsed_left or not cand_left:
        return False
    return parsed_left != cand_left


def _field_bonus(parsed: ParsedCard, candidate: dict[str, Any]) -> float:
    """Award up to 1.0 for matching structured fields between parse+card.

    Fields are weighted because matching set code is much stronger
    evidence than matching the year alone.
    """
    bonus = 0.0
    # Set code (case-insensitive). Strong signal.
    if _set_code_matches(parsed, candidate):
        bonus += 0.5
    # Card number ("123/198" → match against the candidate "number" field).
    if _number_matches(parsed, candidate):
        bonus += 0.3
    # HP (Pokémon). Tolerate ±10 because OCR sometimes misreads 100 as 700.
    if parsed.hp is not None:
        cand_hp_raw = candidate.get("hp") or candidate.get("hit_points")
        try:
            cand_hp = int(cand_hp_raw) if cand_hp_raw is not None else None
        except (TypeError, ValueError):
            cand_hp = None
        if cand_hp is not None and abs(cand_hp - parsed.hp) <= 10:
            bonus += 0.15
    # Year match (loose — within 2 years).
    if parsed.year is not None:
        cand_year_raw = candidate.get("year") or (
            (candidate.get("set") or {}).get("releaseDate")
        )
        try:
            cand_year = int(str(cand_year_raw)[:4]) if cand_year_raw else None
        except (TypeError, ValueError):
            cand_year = None
        if cand_year and abs(cand_year - parsed.year) <= 2:
            bonus += 0.05
    return min(1.0, bonus)


def score_candidate(
    *,
    parsed: ParsedCard,
    candidate: dict[str, Any],
    ocr_confidence: float,
    phash_hit: bool,
    feedback_prior: float = 0.0,
) -> ScoreBreakdown:
    """Compute the weighted confidence for a single candidate.

    All inputs are pure data so this function is deterministic and
    cheaply unit-testable.
    """
    # Use the best of the candidate names against the best of the parsed
    # titles. Both are short strings; comparison is O(small).
    cand_name = candidate.get("name") or ""
    titles = [t for t, _ in parsed.title_candidates] or (
        [parsed.title] if parsed.title else []
    )
    text_sim = max((_string_similarity(t, cand_name) for t in titles), default=0.0)
    field_match = _field_bonus(parsed, candidate)
    clamped_ocr = max(0.0, min(1.0, ocr_confidence))
    clamped_prior = max(0.0, min(1.0, feedback_prior))
    final = (
        _W_TEXT * text_sim
        + _W_OCR * clamped_ocr
        + _W_FIELDS * field_match
        + _W_PHASH * (1.0 if phash_hit else 0.0)
        + _W_FEEDBACK * clamped_prior
    )
    # Synergy bonus: a near-exact title match *and* an exact collector
    # number is a near-certain identification — that pair pins a single
    # printing. The plain weighted sum tops out around 0.64 for such a
    # match (OCR confidence is never 1.0, set codes rarely OCR cleanly on
    # vintage cards), which sits below the client lock threshold and made
    # the scanner feel like it "never recognised" obvious cards. The
    # bonus lifts a confirmed match clear of the carousel noise so it
    # locks, while cards that share a name but not the number stay put.
    if text_sim >= 0.85 and _number_matches(parsed, candidate):
        final += _SYNERGY_TITLE_NUMBER
    elif _set_code_matches(parsed, candidate) and _number_matches(parsed, candidate):
        # Name read poorly, but two unique structured identifiers agree —
        # still a near-certain printing, so lift it clear of the carousel.
        final += _SYNERGY_SET_NUMBER
    # Number conflict guard: if we read a number off the card and the
    # candidate's printed number contradicts it, this is almost certainly
    # the wrong card (a fuzzy name look-alike from another set). Push it
    # well below the lock threshold no matter how close the name reads, so
    # the scanner never commits to e.g. "Energizer #285" for a "Pikachu
    # #058" frame. A phash hit is trusted more than OCR, so soften the
    # penalty there to avoid fighting a strong image-hash identification.
    if _number_conflicts(parsed, candidate):
        final -= _NUMBER_CONFLICT_PENALTY * (0.4 if phash_hit else 1.0)
    return ScoreBreakdown(
        text_similarity=round(text_sim, 4),
        ocr_quality=round(clamped_ocr, 4),
        field_match=round(field_match, 4),
        phash_match=1.0 if phash_hit else 0.0,
        feedback_prior=round(clamped_prior, 4),
        final=round(max(0.0, min(1.0, final)), 4),
    )


__all__ = ["ScoreBreakdown", "infer_tcg", "score_candidate"]
