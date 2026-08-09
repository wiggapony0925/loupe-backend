"""Which hashtags become browsable.

**Why this exists separately from the classifier.** `moderation.py` judges
whether a piece of writing is publishable. Profanity mostly isn't a
moderation category — "#fuck" is rude, not hateful, so the classifier
passes it and should. But a hashtag is not just a word in a sentence: it
becomes a **page**, a **trending chip** and an **autocomplete suggestion**.
Promoting "#fuck" into product furniture is a different act from letting
someone swear in a caption, and it deserves a different rule.

**So the rule is about indexing, not publishing.** A post keeps whatever
its author wrote — the caption is theirs. The *tag* simply doesn't get a
row, which means:

* no tag page and no trending chip;
* the composer never suggests it;
* the client stops linking it automatically — `PostCaption` only styles
  words the server returned in `post.hashtags`, so a tag we didn't index
  renders as ordinary text with no extra client code.

Deliberately a **wordlist**, which `moderation.py` argues against for
content. The objection there was that lists are English-only, evadable and
fire on ordinary collector vocabulary. None of that bites here: a tag is a
single token with no context to misread, the list only decides whether a
word gets its own page (never whether someone may speak), and evading it by
misspelling produces a tag nobody else is searching for — which is its own
punishment.
"""

from __future__ import annotations

import re

#: Tags that never become pages. Kept to slurs, sexual terms and plain
#: profanity — the words that would be indefensible sitting in a trending
#: rail on a product teenagers use. Substring-matched, so "#fuckthis" and
#: "#asshole" are caught by their roots.
BLOCKED_ROOTS: frozenset[str] = frozenset(
    {
        # Profanity
        "fuck",
        "shit",
        "bitch",
        "cunt",
        "bastard",
        "asshole",
        "dickhead",
        "wanker",
        "twat",
        # Sexual
        "porn",
        "nsfw",
        "nude",
        "nudes",
        "boobs",
        "tits",
        "sex",
        "xxx",
        "hentai",
        "onlyfans",
        "escort",
        # Slurs and hate
        "nigg",
        "fag",
        "retard",
        "tranny",
        "kike",
        "spic",
        "chink",
        "wetback",
        # Self-harm / drugs, where a browsable page is the harm
        "suicide",
        "selfharm",
        "cutting",
        "cocaine",
        "meth",
        "heroin",
    }
)

#: Words that CONTAIN a blocked root but are entirely innocent. Checked
#: first, because "#assassin" and "#scunthorpe" are the oldest bug in
#: filtering and a card app really does say "#analysis".
ALLOWED_EXACT: frozenset[str] = frozenset(
    {
        "analysis",
        "assassin",
        "assassins",
        "assess",
        "assessment",
        "bass",
        "class",
        "classic",
        "grass",
        "pass",
        "passion",
        "sussex",
        "essex",
        "middlesex",
        "scunthorpe",
        "shitake",
        "titan",
        "titans",
        "titanium",
        "cocktail",
        "methodical",
        "method",
        "sextet",
        "sexton",
    }
)

#: A tag has to look like a tag. One character is noise, and something 64
#: characters long is somebody testing the input.
MIN_LENGTH = 2
MAX_LENGTH = 64

_WORD = re.compile(r"^[a-z0-9_]+$")


def is_indexable(tag: str) -> bool:
    """Whether ``tag`` may become a page, a chip and a suggestion.

    ``tag`` is expected lowercase and without the leading ``#`` (which is
    what :func:`app.social.services.feed_common.extract_hashtags` produces).
    """
    word = tag.strip().lstrip("#").lower()
    if not (MIN_LENGTH <= len(word) <= MAX_LENGTH):
        return False
    if not _WORD.match(word):
        return False
    if word in ALLOWED_EXACT:
        return True
    # Digits-only tags ("#1", "#2024") are legitimate; the roots are words.
    return not any(root in word for root in BLOCKED_ROOTS)


def indexable(tags: list[str]) -> list[str]:
    """Filter a caption's tags to the ones that earn a page. Order kept."""
    return [tag for tag in tags if is_indexable(tag)]


__all__ = [
    "ALLOWED_EXACT",
    "BLOCKED_ROOTS",
    "indexable",
    "is_indexable",
]
