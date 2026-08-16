"""Content screening for the community feed.

**Why this shape.** Three approaches were on the table:

* a **wordlist** (`better-profanity`, `profanity-check`) — English-only,
  trivially defeated by "f u c k", and it fires on "this pull is sick".
  Worse, it cannot see images at all, and images are most of the risk;
* a **paid vision vendor** (Hive, Sightengine) — excellent, but a new
  contract, a new secret, and per-call cost for a feature with no revenue;
* **OpenAI's moderation endpoint** — free, no separate contract, classifies
  TEXT AND IMAGES in one call across the categories that actually matter
  (sexual, sexual/minors, hate, harassment, violence, self-harm, illicit),
  and we already inject ``OPENAI_API_KEY`` into loupe-api for the carousel
  designer. That is what this uses.

**The policy.** Screening is advisory except where it isn't:

* a hit in :data:`ZERO_TOLERANCE` **blocks the write** — the user gets a
  422 and nothing is stored;
* a category scoring at or above :data:`BLOCK_SCORE` **blocks the write**
  too, whatever the category. A classifier returning 0.97 on hate is not
  making a judgement call, and sending that to a human queue means the
  slur is live until somebody gets to it — hours on a weekday, days over a
  weekend. This is the line between "a machine noticed" and "a machine is
  certain";
* anything else the classifier flags **publishes and opens a case** for a
  human. A collector app's vocabulary is full of "sick", "insane", "killer"
  and "steal"; auto-deleting on a classifier's say-so would delete real
  posts every day, and that uncertain band is exactly where the false
  positives live;
* everything else passes silently.

**Not every surface gets the same policy.** A caption can be published and
reviewed later. A username cannot — by the time anyone opens the queue it is
already on every byline, comment and follower row the account appears in. So
identity text (handles, bios, collection names) is screened with
:data:`IDENTITY`, which refuses anything that trips at all. Nobody is owed
their first choice of username.

The numbers here mirror ``moderato``'s ``POLICY_PRESETS`` on the client.
That is deliberate: the app's preflight and this refusal have to agree, or
users get told something is fine and then refused anyway.

**Failure is open, but never silent.** If the provider errors, times out, or
no key is configured, the post goes through AND a case is opened for review.
The alternative — refusing to publish while a third party is down — turns
someone else's outage into ours. The alternative to the *case* is content
nobody ever looked at.
"""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("social.moderation")

#: The endpoint's model. "omni" is the one that accepts images as well as text.
MODEL = "omni-moderation-latest"

#: A hit here is refused outright. Deliberately short: these are the
#: categories where "publish it and review later" is not an acceptable
#: position to have taken, legally or morally.
ZERO_TOLERANCE = frozenset(
    {
        "sexual/minors",
        "sexual",
        "hate/threatening",
        "harassment/threatening",
        "violence/graphic",
        "self-harm/instructions",
        "illicit/violent",
    }
)

#: Score above which a category counts even when the provider didn't flag
#: it. The endpoint is tuned for general chat; a marketplace where strangers
#: transact wants a slightly lower bar for a *human* to glance at something.
REVIEW_SCORE = 0.55

#: Score at or above which ANY category is refused outright, zero-tolerance
#: or not. Above this the classifier is not making a judgement call, and
#: routing it to a human means the content is live in the meantime.
BLOCK_SCORE = 0.92

#: The documented category names, in the spelling the policy is written in.
#:
#: The SDK hands us *field* names ("self_harm_instructions"), and the old
#: canonicaliser just swapped "_" for "/" — which turns that into
#: "self/harm/instructions" and matches nothing, so a zero-tolerance entry
#: spelled "self-harm/instructions" never fired. Comparison now ignores
#: punctuation entirely and the documented spelling is looked up, so both
#: sides can be written however reads best.
CATEGORY_NAMES: tuple[str, ...] = (
    "harassment",
    "harassment/threatening",
    "hate",
    "hate/threatening",
    "illicit",
    "illicit/violent",
    "self-harm",
    "self-harm/intent",
    "self-harm/instructions",
    "sexual",
    "sexual/minors",
    "violence",
    "violence/graphic",
)


def _key(name: str) -> str:
    """A spelling-insensitive comparison key: letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


_BY_KEY: dict[str, str] = {_key(name): name for name in CATEGORY_NAMES}


def canonical(name: str) -> str:
    """The documented spelling of a category, however it arrived."""
    return _BY_KEY.get(_key(name), name.replace("_", "/"))


@dataclass(frozen=True)
class Policy:
    """How a verdict is read off a classifier result, for one kind of surface."""

    zero_tolerance: frozenset[str] = ZERO_TOLERANCE
    review_score: float = REVIEW_SCORE
    block_score: float = BLOCK_SCORE


#: Feeds, comments, captions, reviews. Refuse the indefensible and the
#: near-certain; queue the doubtful; let the community talk like a community.
BALANCED = Policy()

#: Handles, bios, collection names — permanent, public, and attached to
#: every row the account touches. There is no "review it later" here, so
#: anything that trips at all is refused and the author picks another name.
IDENTITY = Policy(
    zero_tolerance=ZERO_TOLERANCE | {"hate", "harassment"},
    review_score=0.4,
    block_score=0.4,
)

#: Wall-clock budget. Posting must stay fast; a slow classifier degrades to
#: "publish and review", which is the same path as an outage.
TIMEOUT_SECONDS = 6.0

ALLOW = "allow"
REVIEW = "review"
BLOCK = "block"

#: Every refusal the product can utter, in ONE place. Clients render these
#: verbatim (the moderato hook surfaces the 422 detail as-is) — no client
#: invents moderation copy, so changing the voice of a refusal is a backend
#: edit, not an app release. Keyed by the surface passed to
#: ``safety.enforce``; "avatar" is the one override a caller passes
#: explicitly (the profile surface screens both text and picture).
REFUSALS: dict[str, str] = {
    "post": (
        "This post looks like it breaks the community rules. "
        "Loupe is for trading cards — keep it about the cards."
    ),
    "comment": (
        "That comment looks like it breaks the community rules. "
        "Keep it about the cards."
    ),
    "profile": (
        "That profile text looks like it breaks the community rules. "
        "Keep your handle and bio about you and your collection."
    ),
    "avatar": (
        "That picture looks like it breaks the community rules. "
        "Try a photo of you or your collection."
    ),
}


def refusal_for(surface: str) -> str:
    """The copy a refused write on ``surface`` returns to its author."""
    return REFUSALS.get(surface) or Verdict().message()


@dataclass(frozen=True)
class Verdict:
    """What screening decided, and enough detail to explain it later."""

    action: str = ALLOW
    #: Categories that tripped, worst first.
    categories: list[str] = field(default_factory=list)
    #: Highest category score seen (0-1), for ranking the review queue.
    score: float = 0.0
    #: Human-readable note stored on the case.
    detail: str | None = None

    @property
    def blocked(self) -> bool:
        return self.action == BLOCK

    @property
    def needs_review(self) -> bool:
        return self.action in (BLOCK, REVIEW)

    def message(self) -> str:
        """What the author is told. Names the policy, not the classifier —
        a raw category list reads as an accusation and teaches evasion."""
        return (
            "This post looks like it breaks the community rules. "
            "Loupe is for trading cards — keep it about the cards."
        )


def enabled() -> bool:
    """Screening is on when a provider key exists."""
    return bool(get_settings().openai_api_key)


async def screen(
    text: str | None = None,
    images: list[tuple[bytes, str]] | None = None,
    *,
    policy: Policy = BALANCED,
) -> Verdict:
    """Screen a caption and/or its images in ONE call.

    ``images`` is ``[(bytes, content_type), …]``. Returns ALLOW when there is
    nothing to screen; never raises.
    """
    parts: list[Any] = []
    if text and text.strip():
        parts.append({"type": "text", "text": text.strip()})
    for body, content_type in images or []:
        parts.append(
            {
                "type": "image_url",
                "image_url": {
                    # Data URI rather than a public URL: at write time the
                    # bytes aren't served yet, and we shouldn't need them to
                    # be reachable by a third party to check them.
                    "url": f"data:{content_type};base64,"
                    f"{base64.b64encode(body).decode()}"
                },
            }
        )
    if not parts:
        return Verdict()

    # NO KEY IS NOT THE SAME AS A FAILED CHECK. An unconfigured environment
    # (dev, a self-hosted deploy) has screening switched OFF on purpose, and
    # queueing every post there would make the review queue 100% of the
    # content — noise a moderator learns to ignore, which is worse than no
    # queue at all. User reports still work. A vendor that we *tried* and
    # couldn't reach is the case worth flagging, and that's handled below.
    if not enabled():
        return Verdict()

    try:
        return await asyncio.wait_for(
            _classify(parts, policy), timeout=TIMEOUT_SECONDS
        )
    except TimeoutError:
        logger.warning("moderation timed out after %ss", TIMEOUT_SECONDS)
        return Verdict(action=REVIEW, detail="Screening timed out.")
    except Exception:
        logger.exception("moderation call failed")
        return Verdict(action=REVIEW, detail="Screening failed.")


async def _classify(parts: list[Any], policy: Policy = BALANCED) -> Verdict:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=get_settings().openai_api_key)
    # `parts` is built to the documented multi-modal shape; the SDK's
    # TypedDicts don't accept a plain list literal, and building them would
    # drag SDK types into this module's signature for no runtime gain.
    resp = await client.moderations.create(model=MODEL, input=parts)
    result = resp.results[0]

    raw_scores: dict[str, float] = dict(result.category_scores or {})
    raw_flags: dict[str, bool] = dict(result.categories or {})

    # One canonicalised score map, built once — the SDK spells attributes
    # "sexual_minors" and the policy is written "sexual/minors".
    scores: dict[str, float] = {}
    for name, score in raw_scores.items():
        key = canonical(name)
        scores[key] = max(scores.get(key, 0.0), float(score or 0.0))

    names = {canonical(name) for name, hit in raw_flags.items() if hit}
    names |= {name for name, score in scores.items() if score >= policy.review_score}
    tripped = sorted(names, key=lambda name: -scores.get(name, 0.0))
    worst = max(scores.values(), default=0.0)

    zero_hit = next((name for name in tripped if name in policy.zero_tolerance), None)
    if zero_hit is not None:
        return Verdict(
            action=BLOCK,
            categories=tripped,
            score=worst,
            detail=f"Auto-blocked ({zero_hit}): {', '.join(tripped)}",
        )

    certain = next(
        (name for name in tripped if scores.get(name, 0.0) >= policy.block_score),
        None,
    )
    if certain is not None:
        return Verdict(
            action=BLOCK,
            categories=tripped,
            score=worst,
            detail=f"Auto-blocked ({certain} at {scores[certain]:.2f})",
        )

    if tripped:
        return Verdict(
            action=REVIEW,
            categories=tripped,
            score=worst,
            detail=f"Auto-flagged: {', '.join(tripped)}",
        )
    return Verdict(score=worst)


__all__ = [
    "ALLOW",
    "BALANCED",
    "BLOCK",
    "BLOCK_SCORE",
    "CATEGORY_NAMES",
    "IDENTITY",
    "MODEL",
    "REVIEW",
    "REVIEW_SCORE",
    "ZERO_TOLERANCE",
    "Policy",
    "Verdict",
    "canonical",
    "enabled",
    "screen",
]
