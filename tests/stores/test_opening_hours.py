"""OSM ``opening_hours`` → a readable week.

The app used to render the raw expression, which is a wall of text where the
customer wants one thing: are they open, and when. The grammar is large, so
the contract here is deliberately modest and strictly enforced: understand
the common shapes, and NEVER lose or invent information for the rest.
"""

from __future__ import annotations

import pytest

from app.services.stores import opening_hours as oh


def week(raw):
    parsed = oh.parse(raw)
    assert parsed is not None
    return {d.short: d for d in parsed.days}, parsed


# ── The common shapes ──


def test_a_weekday_range_expands_to_each_day():
    days, _ = week("Mo-Fr 09:00-17:00")
    for short in ("Mon", "Tue", "Wed", "Thu", "Fri"):
        assert days[short].ranges == ["09:00-17:00"]
    assert days["Sat"].unknown and days["Sun"].unknown


def test_the_week_is_always_seven_days_monday_first():
    _, parsed = week("Mo 09:00-17:00")
    assert [d.short for d in parsed.days] == [
        "Mon",
        "Tue",
        "Wed",
        "Thu",
        "Fri",
        "Sat",
        "Sun",
    ]


def test_several_rules_combine():
    days, _ = week("Mo-Th 11:00-21:00; Fr-Sa 11:00-22:00; Su 12:00-18:00")
    assert days["Tue"].ranges == ["11:00-21:00"]
    assert days["Fri"].ranges == ["11:00-22:00"]
    assert days["Sun"].ranges == ["12:00-18:00"]


def test_a_day_list_is_expanded():
    days, _ = week("Mo,We,Fr 10:00-18:00")
    assert days["Mon"].ranges == ["10:00-18:00"]
    assert days["Wed"].ranges == ["10:00-18:00"]
    assert days["Tue"].ranges == []
    assert days["Tue"].unknown


def test_split_shifts_keep_both_spans():
    """A lunch break is two spans on one day, not a mangled single one."""
    days, _ = week("Mo-Fr 09:00-12:00,13:00-17:00")
    assert days["Mon"].ranges == ["09:00-12:00", "13:00-17:00"]


def test_a_wrapping_day_range_reads_as_a_human_reads_it():
    days, _ = week("Fr-Mo 10:00-16:00")
    for short in ("Fri", "Sat", "Sun", "Mon"):
        assert days[short].ranges == ["10:00-16:00"], short
    assert days["Wed"].unknown


def test_a_bare_time_span_applies_to_the_whole_week():
    days, _ = week("10:00-18:00")
    assert all(d.ranges == ["10:00-18:00"] for d in days.values())


def test_24_7():
    _, parsed = week("24/7")
    assert parsed.always_open
    assert all(d.ranges == ["Open 24 hours"] for d in parsed.days)


# ── "Closed", holidays, and the words the user asked about ──


@pytest.mark.parametrize("word", ["off", "closed", "Closed", "CLOSED", "shut"])
def test_a_closed_day_is_closed_not_unknown(word):
    days, _ = week(f"Mo-Sa 09:00-17:00; Su {word}")
    assert days["Sun"].ranges == []
    assert days["Sun"].unknown is False, "we were told; that is not the same as unknown"


def test_public_holidays_become_a_note_not_a_day():
    """PH is a calendar qualifier — it doesn't belong in a 7-day grid."""
    _, parsed = week("Mo-Fr 09:00-17:00; PH off")
    assert parsed.notes == ["Public holidays: closed"]
    assert len(parsed.days) == 7


def test_school_holidays_are_kept_too():
    _, parsed = week("Mo-Fr 09:00-17:00; SH 10:00-14:00")
    assert parsed.notes == ["School holidays: 10:00-14:00"]


def test_a_spelled_out_holiday_word_is_understood():
    _, parsed = week("Mo-Fr 09:00-17:00; holidays closed")
    assert parsed.notes == ["Holidays: closed"]


# ── Never lose, never invent ──


def test_a_day_nobody_mentioned_is_UNKNOWN_not_closed():
    """Claiming a shop is shut when the data never said sends someone on a
    wasted trip. Silence and "closed" are different facts."""
    days, _ = week("Mo-Fr 09:00-17:00")
    assert days["Sun"].unknown is True
    assert days["Sun"].ranges == []


def test_an_unparseable_rule_is_kept_verbatim():
    _, parsed = week("Mo-Fr 09:00-17:00; by appointment only")
    assert "by appointment only" in parsed.notes


def test_odd_times_pass_through_unchanged():
    """Reformatting "sunset" into a clock time invents precision."""
    days, _ = week("Mo-Su sunrise-sunset")
    assert days["Mon"].ranges == ["sunrise-sunset"]


def test_the_original_string_is_always_returned():
    raw = "Mo-Fr 09:00-17:00; PH off"
    _, parsed = week(raw)
    assert parsed.raw == raw


@pytest.mark.parametrize("junk", [None, "", "   "])
def test_nothing_in_nothing_out(junk):
    assert oh.parse(junk) is None


def test_a_completely_unreadable_string_still_surfaces_something():
    """Better a note the customer can read than a blank panel."""
    parsed = oh.parse("ring the bell")
    assert parsed is not None
    assert parsed.notes == ["ring the bell"]


def test_duplicate_spans_are_not_repeated():
    days, _ = week("Mo 09:00-17:00; Mo 09:00-17:00")
    assert days["Mon"].ranges == ["09:00-17:00"]


# ── Strings taken from LIVE OSM data ──
#
# These two were parsed WRONG by the first version of this module and were
# only caught by running it over real card shops. Synthetic cases all passed.
# Real data uses commas where the spec wants semicolons, and separates split
# shifts with spaces.


def test_real_the_uncommons_comma_separated_rules():
    days, _ = week("Mo-Th 10:00-23:00, Fr-Sa 10:00-24:00, Su 10:00-23:00")
    assert days["Thu"].ranges == ["10:00-23:00"]
    assert days["Fri"].ranges == ["10:00-24:00"]
    assert days["Sat"].ranges == ["10:00-24:00"]
    assert days["Sun"].ranges == ["10:00-23:00"]


def test_real_warhammer_space_split_shift_and_trailing_closed():
    days, _ = week("We-Sa 11:00-15:30 16:00-19:00, Su 11:00-17:00, Mo-Tu closed")
    assert days["Wed"].ranges == ["11:00-15:30", "16:00-19:00"]
    assert days["Sun"].ranges == ["11:00-17:00"]
    for shut in ("Mon", "Tue"):
        assert days[shut].ranges == []
        assert days[shut].unknown is False, "explicitly closed, not unknown"


def test_a_day_LIST_still_is_not_treated_as_a_rule_separator():
    """The comma fix must not break "Mo,We,Fr" or "09:00-12:00,13:00-17:00"."""
    days, _ = week("Mo,We,Fr 10:00-18:00")
    assert days["Mon"].ranges == ["10:00-18:00"]
    assert days["Tue"].unknown
    days2, _ = week("Mo-Fr 09:00-12:00,13:00-17:00")
    assert days2["Mon"].ranges == ["09:00-12:00", "13:00-17:00"]
