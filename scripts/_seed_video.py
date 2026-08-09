"""Build short MP4 clips for the seed, locally, from images we already have.

**Why generate rather than download.** Wikimedia Commons — the source for
every other image in the seed — does not host MP4: their policy is patent-
free formats, so their video is WebM and Ogg. This app deliberately refuses
WebM (iOS cannot play it), so seeding from Commons video would fill the feed
with clips half the users can't watch. Pulling MP4s from some other host
means re-hosting files with no licence anyone could point at.

So the clips are made here from the SAME licensed stills the seed already
uses: a slow pan across a card or a photograph, which is what a real "look
at this" clip of a card looks like anyway. The licence is unchanged because
the source image is unchanged.

Needs ffmpeg on PATH. It is a dev-only dependency of a dev-only script —
the API neither transcodes nor inspects video beyond reading two integers
out of a header. If ffmpeg is missing, `clips()` returns an empty list and
the seed carries on with photos, because a missing test video should never
be the reason a seed run fails.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger("seed.video")

#: Short enough to loop pleasantly in a feed, long enough to be a video.
CLIP_SECONDS = 6
#: Portrait, phone-shaped. A landscape clip in a portrait feed reads as an
#: import from somewhere else.
WIDTH, HEIGHT = 720, 1280


def available() -> bool:
    return shutil.which("ffmpeg") is not None


async def clips(images: list[bytes], *, limit: int = 6) -> list[bytes]:
    """Render up to `limit` MP4s, one per source image."""
    if not available():
        logger.warning("ffmpeg not on PATH — seeding photos only, no video")
        return []

    out: list[bytes] = []
    for index, image in enumerate(images[:limit]):
        clip = await asyncio.to_thread(_render, image, index)
        if clip:
            out.append(clip)
    logger.info("built %d clips", len(out))
    return out


def _render(image: bytes, index: int) -> bytes | None:
    """One still → one panning clip.

    `zoompan` does the Ken Burns move. The scale-then-crop before it is not
    decoration: zoompan works on the input frame, so a small source image
    would produce a clip the size of the source, and the pan would be a few
    pixels wide.

    ``+faststart`` moves the moov atom to the front, which is what lets a
    player begin before the whole file has arrived — and what lets the
    dimension probe find `tkhd` inside its 512 KB window.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "in.img"
        target = Path(tmp) / "out.mp4"
        source.write_bytes(image)

        # Alternate the pan direction so a feed of these doesn't look like
        # one clip posted six times.
        drift = "iw/2-(iw/zoom/2)" if index % 2 == 0 else "iw-(iw/zoom)"
        frames = CLIP_SECONDS * 30

        command = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-i",
            str(source),
            "-vf",
            (
                f"scale={WIDTH * 2}:{HEIGHT * 2}:force_original_aspect_ratio=increase,"
                f"crop={WIDTH * 2}:{HEIGHT * 2},"
                f"zoompan=z='min(zoom+0.0012,1.3)':x='{drift}':y='ih/2-(ih/zoom/2)'"
                f":d={frames}:s={WIDTH}x{HEIGHT}:fps=30,"
                "format=yuv420p"
            ),
            "-t",
            str(CLIP_SECONDS),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "26",
            # yuv420p + baseline-friendly settings: what every phone decodes.
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning("ffmpeg failed on clip %d (%s)", index, exc)
            return None
        return target.read_bytes() if target.exists() else None


__all__ = ["CLIP_SECONDS", "available", "clips"]
