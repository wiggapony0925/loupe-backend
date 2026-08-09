"""Phone normalisation — the UNIQUE index is only as good as this function."""

from __future__ import annotations

import pytest

from app.utils.phone import InvalidPhone, mask, normalise


@pytest.mark.parametrize(
    "raw",
    [
        "+14155550123",
        "4155550123",
        "(415) 555-0123",
        "415-555-0123",
        "415.555.0123",
        "  +1 415 555 0123  ",
        "14155550123",
        "0014155550123",
    ],
)
def test_every_way_a_person_writes_one_number_is_one_string(raw):
    """If these disagreed the UNIQUE index would be decoration: the same
    person could hold three accounts, and "is this taken?" would answer no
    when it means yes."""
    assert normalise(raw) == "+14155550123"


def test_a_written_country_code_is_taken_at_its_word():
    assert normalise("+44 20 7946 0958") == "+442079460958"
    assert normalise("0044 20 7946 0958") == "+442079460958"


@pytest.mark.parametrize(
    "raw", ["", "   ", "abc", "555", "+1", "1" * 20, "+1415+5550123", "()-."]
)
def test_junk_is_rejected_rather_than_stored(raw):
    with pytest.raises(InvalidPhone):
        normalise(raw)


def test_mask_shows_enough_to_recognise_and_no_more():
    assert mask("+14155550123") == "+1 ••• ••• 0123"
    assert mask(None) is None
