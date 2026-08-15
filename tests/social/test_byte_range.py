"""Range requests, pinned — because getting these wrong makes video unplayable.

Every video on the feed was dead, and this is the layer that killed them. The
symptom was silent: no client error, no server error, the clip simply never
started, so the failure looked like a player bug for far longer than it should
have. These tests exist so it cannot come back quietly.

The first test is the actual regression: the two-byte probe iOS opens with.
"""

from __future__ import annotations

import pytest

from app.social.byte_range import parse_range, ranged_response

BODY = bytes(range(256))  # 256 bytes, value == index, so slices self-verify


def test_the_probe_ios_opens_with_gets_a_206():
    """THE REGRESSION. AVPlayer sends `Range: bytes=0-1` and decides from the
    reply whether this origin can seek. A 200 here means no video ever plays."""
    resp = ranged_response(BODY, "video/mp4", "bytes=0-1")

    assert resp.status_code == 206, (
        "a ranged request was answered with the whole body — iOS reads that as "
        "an origin that cannot serve ranges and refuses to play the video"
    )
    assert resp.body == BODY[0:2]
    assert resp.headers["content-range"] == "bytes 0-1/256"
    assert resp.headers["accept-ranges"] == "bytes"


def test_a_request_with_no_range_still_gets_the_whole_body():
    """An <img> never sends Range. Images must keep working exactly as before."""
    resp = ranged_response(BODY, "image/jpeg", None)

    assert resp.status_code == 200
    assert resp.body == BODY
    assert resp.headers["accept-ranges"] == "bytes"


def test_accept_ranges_is_advertised_on_every_reply():
    """The stories route advertised this header while ignoring Range entirely.
    Advertising it is what obliges the server to honour it, so the two must
    never drift apart again."""
    for header in (None, "bytes=0-1", "bytes=10-20"):
        assert ranged_response(BODY, "video/mp4", header).headers["accept-ranges"] == (
            "bytes"
        )


def test_an_open_ended_range_runs_to_the_end():
    resp = ranged_response(BODY, "video/mp4", "bytes=200-")

    assert resp.status_code == 206
    assert resp.body == BODY[200:]
    assert resp.headers["content-range"] == "bytes 200-255/256"


def test_a_suffix_range_reads_the_END_of_the_file():
    """`bytes=-16` means the LAST 16 bytes. Reading it as a prefix is the
    subtle way to break this: an MP4 with its moov atom at the end — which is
    what a phone records — would be handed its header where it asked for the
    trailer, and never become playable."""
    resp = ranged_response(BODY, "video/mp4", "bytes=-16")

    assert resp.status_code == 206
    assert resp.body == BODY[-16:], "a suffix range was served from the front"
    assert resp.headers["content-range"] == "bytes 240-255/256"


def test_a_range_past_the_end_is_clamped_not_rejected():
    """Players routinely ask for more than is there. RFC 7233 says clamp."""
    resp = ranged_response(BODY, "video/mp4", "bytes=250-9999")

    assert resp.status_code == 206
    assert resp.body == BODY[250:]
    assert resp.headers["content-range"] == "bytes 250-255/256"


def test_a_start_beyond_the_file_is_a_416_carrying_the_real_size():
    """A bare 416 gives the client nothing to retry with."""
    resp = ranged_response(BODY, "video/mp4", "bytes=999-1000")

    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */256"


@pytest.mark.parametrize(
    "header",
    ["", "bytes=", "bytes=abc-def", "items=0-10", "0-10", "bytes=0-99,200-299"],
)
def test_anything_unparseable_degrades_to_the_full_body(header):
    """RFC 7233: an unsatisfiable-because-malformed Range MUST be ignored, not
    rejected. Multi-range is in this list on purpose — we do not implement
    multipart/byteranges, and answering one with a clean 200 is far better than
    answering it with a malformed multipart body."""
    resp = ranged_response(BODY, "video/mp4", header)

    assert resp.status_code == 200
    assert resp.body == BODY


def test_an_empty_body_never_produces_a_partial_response():
    """Guards a div-by-zero-shaped edge: size 0 has no valid range at all."""
    resp = ranged_response(b"", "video/mp4", "bytes=0-1")

    assert resp.status_code == 200
    assert resp.body == b""


def test_the_content_type_survives_a_ranged_reply():
    """A 206 that loses `video/mp4` is a 206 the player will not use."""
    resp = ranged_response(BODY, "video/quicktime", "bytes=0-1")

    assert resp.headers["content-type"] == "video/quicktime"


def test_parse_range_returns_inclusive_offsets():
    """Documenting the boundary directly: HTTP ranges are inclusive at BOTH
    ends, so `bytes=0-1` is two bytes. An exclusive reading is off by one on
    every single request and corrupts the stream rather than failing loudly."""
    assert parse_range("bytes=0-1", 256) == (0, 1)
    assert parse_range("bytes=0-0", 256) == (0, 0)
    assert parse_range(None, 256) is None
