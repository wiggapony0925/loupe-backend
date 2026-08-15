"""HTTP range responses, because iOS will not play a video without them.

THE BUG THIS EXISTS FOR. Every video posted to the feed refused to play — not
some, not intermittently, all of them, on every build. Images from the same
endpoint were fine, which is what made it look like a player problem and kept
attention on the client for far too long.

It was the server. ``AVPlayer`` — the engine behind ``expo-video``, and behind
every native player on iOS — does not fetch a video by simply GETting it. It
opens with a probe for the first couple of bytes::

    GET /v1/social/posts/media/<id>
    Range: bytes=0-1

and reads the reply to decide whether the origin can seek. A server that
supports ranges answers ``206 Partial Content`` with a ``Content-Range``. Ours
answered::

    HTTP/2 200
    content-length: 73168

the whole file, for a request that asked for two bytes. To AVPlayer that is an
origin which cannot serve ranges, so it will not set up progressive playback,
and the clip never starts. No error reaches JS. Nothing is logged. The video
just sits there, which is exactly what was reported.

An ``<img>`` tag never sends Range at all, so images were unaffected and the
endpoint looked healthy from every direction anyone thought to check.

THE STORIES ROUTE WAS WORSE, and worth spelling out because it is the trap this
module closes. It advertised::

    Accept-Ranges: bytes

while still returning the entire body with a 200 to any ranged request. That is
not a partial implementation, it is a false one: the header is a promise that
ranges work, and a client that believes it and then gets a 200 back is in a
state the protocol does not describe. Advertising the capability is what
obliges you to implement it — so both routes now share this code rather than
each hand-rolling a header set.

WHAT IS AND IS NOT HANDLED. Single ranges only. Multi-range requests
(``bytes=0-99,200-299``) need a multipart/byteranges body, no player we serve
ever sends one, and a wrong multipart implementation is a worse failure than
the clean full-body 200 this falls back to. Suffix ranges (``bytes=-500``, the
last 500 bytes) ARE handled: that is how a player reads an MP4's ``moov`` atom
when it sits at the end of the file, which is the usual layout for anything a
phone recorded and did not re-mux.
"""

from __future__ import annotations

from fastapi import Response

__all__ = ["parse_range", "ranged_response"]

#: A year. These bytes are written once under a key derived from the row id and
#: never replaced — see ``post_media.storage_key`` — so nothing needs revalidating.
_CACHE = "public, max-age=31536000, immutable"


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Resolve a Range header to inclusive ``(start, end)`` byte offsets.

    Returns ``None`` when the whole body should be sent — no header, a unit we
    do not speak, a multi-range request, or anything malformed. RFC 7233 is
    explicit that a Range which cannot be parsed MUST be ignored rather than
    rejected, so a bad header degrades to a normal 200 instead of an error.

    Raises :class:`ValueError` only for a syntactically valid range that falls
    outside the file, which the caller turns into a 416.
    """
    if not header or size <= 0:
        return None

    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None

    start_s, sep, end_s = spec.strip().partition("-")
    if not sep:
        return None

    try:
        if not start_s:
            # Suffix: "bytes=-500" is the LAST 500 bytes, not "up to 500".
            # Reading it as a prefix would hand a player the file's header
            # where it asked for the trailer — an MP4 whose moov atom sits at
            # the end would never become playable.
            length = int(end_s)
            if length <= 0:
                raise ValueError("empty suffix range")
            return max(0, size - length), size - 1

        start = int(start_s)
        # An open-ended "bytes=1024-" means "the rest of the file".
        end = int(end_s) if end_s else size - 1
    except ValueError as exc:
        if "empty suffix" in str(exc):
            raise
        return None  # non-numeric: ignore the header, send everything

    if start >= size or start < 0:
        raise ValueError(f"range start {start} outside 0..{size - 1}")

    # A client may ask past the end; the spec says clamp rather than fail.
    return start, min(end, size - 1)


def ranged_response(
    body: bytes, content_type: str, range_header: str | None
) -> Response:
    """Serve ``body``, honouring a Range request if there is one.

    Always advertises ``Accept-Ranges: bytes`` — and, unlike the code this
    replaces, always means it.
    """
    size = len(body)
    headers = {"Cache-Control": _CACHE, "Accept-Ranges": "bytes"}

    try:
        window = parse_range(range_header, size)
    except ValueError:
        # 416 must carry the real size so the client can retry sensibly. A
        # player that gets a bare 416 has no way to recover and gives up.
        return Response(
            status_code=416,
            headers={**headers, "Content-Range": f"bytes */{size}"},
        )

    if window is None:
        return Response(content=body, media_type=content_type, headers=headers)

    start, end = window
    return Response(
        content=body[start : end + 1],
        status_code=206,
        media_type=content_type,
        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"},
    )
