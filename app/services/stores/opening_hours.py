"""Turn an OSM ``opening_hours`` string into a readable week.

OpenStreetMap stores hours as one dense expression:

    Mo-Th 11:00-21:00; Fr-Sa 11:00-22:00; Su 12:00-18:00; PH off

which the app was rendering verbatim — a wall of text where a customer wants
one question answered: *are they open, and when.* This expands it to seven
ordered days so a client can draw a table.

Parsed HERE rather than on each client, per the house rule that the backend
owns logic: web and mobile would otherwise each grow their own half-correct
parser and disagree about the same shop.

The grammar is large and this is a deliberately partial reader. The design
rule is that anything it does NOT understand is preserved as a note rather
than dropped — a shop with unusual hours ends up with less structure, never
with wrong hours. Times are passed through exactly as written (including
``sunset``, ``dusk``): reformatting invents precision we were not given.
"""

from __future__ import annotations

import re

from app.schemas.stores import OpeningHoursDay, OpeningHoursWeek

#: OSM day tokens, in the order a week is read.
DAY_CODES = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
DAY_NAMES = {
    "Mo": ("Monday", "Mon"),
    "Tu": ("Tuesday", "Tue"),
    "We": ("Wednesday", "Wed"),
    "Th": ("Thursday", "Thu"),
    "Fr": ("Friday", "Fri"),
    "Sa": ("Saturday", "Sat"),
    "Su": ("Sunday", "Sun"),
}
_DAY_INDEX = {code: i for i, code in enumerate(DAY_CODES)}

#: Words that mean "not open" in the wild. OSM specifies `off` and `closed`,
#: but real data carries plenty of hand-written variants.
CLOSED_WORDS = {"off", "closed", "close", "shut", "geschlossen", "fermé", "cerrado"}

#: Non-weekday selectors. PH/SH are the spec's holiday tokens; the rest turn
#: up in hand-edited data. These become NOTES — they qualify the week rather
#: than belonging to a day of it.
QUALIFIER_LABELS = {
    "PH": "Public holidays",
    "SH": "School holidays",
    "EASTER": "Easter",
    "HOLIDAY": "Holidays",
    "HOLIDAYS": "Holidays",
}

#: A comma that separates RULES rather than items in a list. Real data uses
#: commas where the spec wants semicolons ("Mo-Th 10:00-23:00, Fr-Sa 10:00-24:00").
#: The discriminator is what sits either side: a comma after a TIME and before
#: a DAY starts a new rule, while "Mo,We,Fr" (letters before) and
#: "09:00-12:00,13:00-17:00" (digits after) are lists and must not split.
_RULE_COMMA = re.compile(r"(?<=\d)\s*,\s*(?=(?:Mo|Tu|We|Th|Fr|Sa|Su)\b)", re.IGNORECASE)

_DAY_TOKEN = re.compile(r"^(Mo|Tu|We|Th|Fr|Sa|Su)$", re.IGNORECASE)
_DAY_RANGE = re.compile(
    r"^(Mo|Tu|We|Th|Fr|Sa|Su)\s*-\s*(Mo|Tu|We|Th|Fr|Sa|Su)$", re.IGNORECASE
)
#: A time span: 09:00-17:00, 9:00-17:00, 09:00+, sunrise-sunset.
_TIME_SPAN = re.compile(
    r"^(?:\d{1,2}:\d{2}|sunrise|sunset|dawn|dusk)\s*"
    r"(?:-\s*(?:\d{1,2}:\d{2}|sunrise|sunset|dawn|dusk)|\+)?$",
    re.IGNORECASE,
)


def _canonical_day(token: str) -> str | None:
    for code in DAY_CODES:
        if code.lower() == token.lower():
            return code
    return None


def _expand_days(selector: str) -> tuple[list[str], str | None]:
    """Day codes a selector covers, plus a qualifier label when it isn't days.

    ``Mo-We,Fr`` → ``["Mo","Tu","We","Fr"]``. Wrapping ranges are honoured:
    ``Sa-Su`` and ``Fr-Mo`` both mean what a human means by them.
    """
    days: list[str] = []
    qualifier: str | None = None

    for part in selector.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        upper = chunk.upper()
        if upper in QUALIFIER_LABELS:
            qualifier = QUALIFIER_LABELS[upper]
            continue
        span = _DAY_RANGE.match(chunk)
        if span:
            start = _canonical_day(span.group(1))
            end = _canonical_day(span.group(2))
            if start is None or end is None:
                continue
            i, j = _DAY_INDEX[start], _DAY_INDEX[end]
            # Fr-Mo wraps the end of the week; a human reads that fine.
            indices = (
                list(range(i, j + 1))
                if i <= j
                else list(range(i, 7)) + list(range(j + 1))
            )
            days.extend(DAY_CODES[k] for k in indices)
            continue
        if _DAY_TOKEN.match(chunk):
            day = _canonical_day(chunk)
            if day:
                days.append(day)
    return days, qualifier


def _split_selector_and_times(rule: str) -> tuple[str, str]:
    """Split ``Mo-Fr 09:00-17:00`` into its day part and its time part.

    The boundary is the first token that looks like a time or a closed word,
    which avoids depending on a fixed number of leading day tokens.
    """
    tokens = rule.split()
    for idx, token in enumerate(tokens):
        cleaned = token.strip(",")
        if cleaned.lower() in CLOSED_WORDS or any(
            _TIME_SPAN.match(p.strip()) for p in cleaned.split(",") if p.strip()
        ):
            return " ".join(tokens[:idx]), " ".join(tokens[idx:])
    return rule, ""


def parse(raw: str | None) -> OpeningHoursWeek | None:
    """Seven ordered days, or ``None`` when there is nothing to show.

    Unrecognised rules land in ``notes`` verbatim, so no information is lost
    on the way to the client.
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()
    days: dict[str, list[str]] = {code: [] for code in DAY_CODES}
    stated: set[str] = set()
    notes: list[str] = []

    # "24/7" is its own whole grammar in practice.
    if re.fullmatch(r"24\s*/\s*7", text) or text.lower() in {"24/7", "always open"}:
        return OpeningHoursWeek(
            days=[
                OpeningHoursDay(
                    day=DAY_NAMES[c][0], short=DAY_NAMES[c][1], ranges=["Open 24 hours"]
                )
                for c in DAY_CODES
            ],
            always_open=True,
            raw=text,
        )

    normalised = _RULE_COMMA.sub("; ", text)
    for rule in normalised.split(";"):
        chunk = rule.strip().rstrip(",")
        if not chunk:
            continue

        selector, times = _split_selector_and_times(chunk)
        day_codes, qualifier = _expand_days(selector)
        is_closed = times.strip().lower().rstrip(".") in CLOSED_WORDS

        # A qualifier (PH/SH) is about the calendar, not the week grid.
        if qualifier:
            notes.append(
                f"{qualifier}: {'closed' if is_closed else (times or 'see store')}"
            )
            continue

        # A bare time span with no day selector applies to the whole week —
        # "10:00-18:00" on its own is a complete, common expression.
        if not day_codes and not selector.strip() and times:
            day_codes = list(DAY_CODES)

        if not day_codes:
            # Understood nothing. Keep it rather than drop it.
            notes.append(chunk)
            continue

        for code in day_codes:
            stated.add(code)
            if is_closed:
                continue
            # Split shifts appear comma-separated AND space-separated.
            spans = [s for s in re.split(r"[,\s]+", times) if s.strip()]
            for span in spans:
                if span not in days[code]:
                    days[code].append(span)

    if not stated and not notes:
        return None

    return OpeningHoursWeek(
        days=[
            OpeningHoursDay(
                day=DAY_NAMES[c][0],
                short=DAY_NAMES[c][1],
                ranges=days[c],
                # A day nobody mentioned is UNKNOWN, not closed — claiming a
                # shop is shut when the data simply didn't say sends someone
                # on a wasted trip.
                unknown=c not in stated,
            )
            for c in DAY_CODES
        ],
        notes=notes,
        raw=text,
    )


__all__ = ["parse"]
