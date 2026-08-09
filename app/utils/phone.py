"""Phone numbers, normalised to E.164 before they ever reach the database.

**Why normalise at all.** "(415) 555-0123", "415-555-0123" and
"+1 415 555 0123" are one line and three strings. Stored raw, the UNIQUE
index on `users.phone` is decoration: the same person registers three
times, a future SMS login can't resolve who to log in, and a
"is this number already taken" check answers no when it means yes.

Deliberately NOT `phonenumbers` (Google's libphonenumber port): it is a
~10 MB dependency carrying a full metadata database for every carrier on
earth, and what this needs is "strip the punctuation, apply a default
country, insist on a plausible length". If Loupe ever needs real carrier
validation or per-region formatting, swap the body of :func:`normalise` —
every caller goes through it.
"""

from __future__ import annotations

import re

#: Where a number with no country code is assumed to be from. The user base
#: is US-first; anyone else can type the "+" and be taken at their word.
DEFAULT_COUNTRY_CODE = "1"

#: E.164 permits at most 15 digits including the country code, and nothing
#: real is shorter than 8.
MIN_DIGITS = 8
MAX_DIGITS = 15

_NON_DIGIT = re.compile(r"[^\d+]")
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class InvalidPhone(ValueError):
    """The input could not be read as a phone number."""


def normalise(raw: str) -> str:
    """Return ``raw`` as E.164 (``+14155550123``), or raise :class:`InvalidPhone`.

    Accepts the shapes people actually type: spaces, dashes, brackets, dots,
    a leading ``+``, a leading ``00`` (the international prefix outside
    NANP), or a bare national number.
    """
    if not raw or not raw.strip():
        raise InvalidPhone("Enter a phone number")

    text = _NON_DIGIT.sub("", raw.strip())

    # "0044…" is how most of the world writes "+44…".
    if text.startswith("00"):
        text = "+" + text[2:]
    # A "+" anywhere but the front is a typo, not a country code.
    if "+" in text[1:]:
        raise InvalidPhone("That doesn't look like a phone number")

    if not text.startswith("+"):
        digits = text
        # A NANP number written with its trunk prefix ("1 415 …").
        if DEFAULT_COUNTRY_CODE == "1" and len(digits) == 11 and digits.startswith("1"):
            text = f"+{digits}"
        else:
            text = f"+{DEFAULT_COUNTRY_CODE}{digits}"

    digits = text[1:]
    if not digits.isdigit():
        raise InvalidPhone("That doesn't look like a phone number")
    if not (MIN_DIGITS <= len(digits) <= MAX_DIGITS):
        raise InvalidPhone("That phone number is the wrong length")
    if not _E164.match(text):
        raise InvalidPhone("That doesn't look like a phone number")
    return text


def mask(e164: str | None) -> str | None:
    """``+14155550123`` → ``+1 ••• ••• 0123``.

    What a user is shown about their OWN number — enough to recognise which
    line it is, not enough to be worth reading off a shoulder. Other users
    never see it in any form.
    """
    if not e164:
        return None
    tail = e164[-4:]
    return f"{e164[:2]} ••• ••• {tail}"


__all__ = ["DEFAULT_COUNTRY_CODE", "InvalidPhone", "mask", "normalise"]
