"""Deterministic, offline generator of realistic card-identification scenarios
for stress-testing the *ranking* engine (``score_candidate`` + sort).

No network, no DB, no OCR provider: each scenario synthesizes a **truth** card,
a pool of adversarial **distractors** (reprints, number-conflict look-alikes,
same-name collisions, random noise), and a **degraded ``ParsedCard``** (OCR
name noise, sometimes-missing set code / collector number — exactly how vintage,
foil, and non-English cards read). This lets us throw *thousands* of payloads at
the ranker in milliseconds and measure top-1 accuracy + latency reproducibly,
the way a real scanner exercises it but without the flakiness of live images.

The generator is intentionally *honest* about ambiguity: a reprint tie with no
readable set code is genuinely undecidable at top-1 (two printings share name +
number), so those scenarios are flagged ``decidable=False`` and scored on
top-2 recall instead — which is precisely what the scanner UI's "which one is
it?" confirm state is for. We never ask the ranker to guess a coin flip.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from app.services.identification.confidence import score_candidate
from app.services.identification.text_parser import ParsedCard

# A spread of plausible card names across TCGs. Short + long, one + two words,
# some sharing prefixes so look-alike distractors are realistic.
_NAMES: tuple[str, ...] = (
    "Pikachu",
    "Raichu",
    "Charizard",
    "Charmander",
    "Charmeleon",
    "Blastoise",
    "Squirtle",
    "Venusaur",
    "Bulbasaur",
    "Mewtwo",
    "Mew",
    "Gengar",
    "Gastly",
    "Snorlax",
    "Lugia",
    "Ho-Oh",
    "Rayquaza",
    "Gardevoir",
    "Lucario",
    "Greninja",
    "Umbreon",
    "Espeon",
    "Sylveon",
    "Dragonite",
    "Dratini",
    "Gyarados",
    "Black Lotus",
    "Lightning Bolt",
    "Counterspell",
    "Dark Ritual",
    "Llanowar Elves",
    "Serra Angel",
    "Shivan Dragon",
    "Birds of Paradise",
    "Tarmogoyf",
    "Snapcaster Mage",
    "Blue-Eyes White Dragon",
    "Dark Magician",
    "Red-Eyes Black Dragon",
    "Exodia the Forbidden One",
    "Summoned Skull",
    "Celtic Guardian",
    "Kuriboh",
    "Time Wizard",
    "Pot of Greed",
    "Elemental HERO Neos",
    "Stardust Dragon",
    "Ash Blossom",
    "Monster Reborn",
)

_SET_CODES: tuple[str, ...] = (
    "BS",
    "JU",
    "FO",
    "TR",
    "GH",
    "N1",
    "N2",
    "N3",
    "EX",
    "SV1",
    "SV3",
    "SWSH1",
    "SM1",
    "XY1",
    "BW1",
    "DP1",
    "LOB",
    "MRD",
    "SRL",
    "PSV",
    "LON",
    "M10",
    "M11",
    "ZEN",
    "ROE",
    "SOM",
    "AVR",
    "RTR",
    "THS",
    "KTK",
)


def _card(
    cid: str,
    name: str,
    number: str | None,
    set_code: str | None,
    *,
    hp: int | None = None,
    tcg: str = "pokemon",
    year: int | None = None,
) -> dict[str, Any]:
    """Build a candidate dict shaped like the catalog rows ``score_candidate``
    consumes (``name`` / ``number`` / nested ``set`` / ``hp`` / ``tcg``)."""
    card: dict[str, Any] = {
        "id": cid,
        "name": name,
        "number": number,
        "tcg": tcg,
        "set": {"code": set_code, "name": f"{set_code} Set" if set_code else None},
    }
    if hp is not None:
        card["hp"] = hp
    if year is not None:
        card["year"] = year
    return card


_OCR_SWAPS = {
    "o": "0",
    "O": "0",
    "l": "1",
    "I": "1",
    "s": "5",
    "S": "5",
    "e": "c",
    "a": "e",
    "n": "m",
    "u": "v",
    "g": "q",
    "t": "f",
}


def _noise_name(name: str, rng: random.Random, *, errors: int = 1) -> str:
    """Introduce 1-2 realistic OCR character errors into a name (substitutions,
    a dropped char) — enough to dent fuzzy similarity without destroying it."""
    chars = list(name)
    alpha_idx = [i for i, c in enumerate(chars) if c.isalpha()]
    if not alpha_idx:
        return name
    for _ in range(errors):
        i = rng.choice(alpha_idx)
        roll = rng.random()
        if roll < 0.6 and chars[i] in _OCR_SWAPS:
            chars[i] = _OCR_SWAPS[chars[i]]
        elif roll < 0.85 and len(chars) > 3:
            chars[i] = ""  # dropped character
        else:
            chars[i] = rng.choice("abcdefghijklmnopqrstuvwxyz")
    return "".join(chars)


@dataclass(frozen=True, slots=True)
class Scenario:
    """One synthetic identify payload: what OCR "saw" + the candidate pool."""

    kind: str
    parsed: ParsedCard
    ocr_confidence: float
    pool: list[dict[str, Any]]
    truth_id: str
    # ``True`` when the parsed signal *uniquely* identifies the truth (top-1
    # must be the truth). ``False`` for a genuine reprint tie with no readable
    # set code — undecidable at top-1, so we require truth in the top-2.
    decidable: bool
    # ``True`` when the pool contains a same-name card with a *different* number
    # than the (clean) parsed number — the number-conflict guard must keep it
    # from ever winning.
    has_conflict_distractor: bool


def _random_distractors(
    rng: random.Random, truth_name: str, count: int
) -> list[dict[str, Any]]:
    """Unrelated cards — different name + number — as background noise."""
    out: list[dict[str, Any]] = []
    for _ in range(count):
        name = rng.choice([n for n in _NAMES if n != truth_name])
        left = rng.randint(1, 250)
        out.append(
            _card(
                f"noise:{name}:{left}:{rng.randint(0, 9999)}",
                name,
                f"{left}/{rng.randint(left, 300)}",
                rng.choice(_SET_CODES),
            )
        )
    return out


def make_scenario(rng: random.Random) -> Scenario:
    """Synthesize one adversarial scenario, weighted toward the hard cases a
    real scanner hits (reprints, look-alikes, degraded names)."""
    name = rng.choice(_NAMES)
    set_code = rng.choice(_SET_CODES)
    num_left = rng.randint(1, 240)
    set_size = rng.randint(num_left, 300)
    number = f"{num_left}/{set_size}"
    hp = rng.choice([None, 60, 70, 90, 110, 120, 150, 200, 220, 300])
    truth = _card(f"truth:{name}:{set_code}:{num_left}", name, number, set_code, hp=hp)

    kind = rng.choices(
        (
            "clean_number",
            "reprint_with_setcode",
            "reprint_no_setcode",
            "noisy_name",
            "name_only",
        ),
        weights=(0.34, 0.18, 0.18, 0.18, 0.12),
    )[0]

    pool: list[dict[str, Any]] = [truth]
    decidable = True
    has_conflict = False
    ocr_conf = round(rng.uniform(0.55, 0.95), 3)

    # Parsed fields start from the truth, then get degraded per scenario kind.
    p_title = name
    p_set: str | None = set_code
    p_number: str | None = number

    if kind == "clean_number":
        # A same-name look-alike with a DIFFERENT number — the classic
        # "Energizer #285 vs Pikachu #058" trap. Number-conflict must sink it.
        other_left = num_left + rng.choice([1, 2, 3, 10, 50, 100])
        pool.append(
            _card(
                f"conflict:{name}:{rng.choice(_SET_CODES)}:{other_left}",
                name,  # identical name → identical text similarity
                f"{other_left}/{set_size}",
                rng.choice(_SET_CODES),
            )
        )
        has_conflict = True

    elif kind in ("reprint_with_setcode", "reprint_no_setcode"):
        # A true reprint: SAME name + SAME number, different set. Only the set
        # code can break the tie.
        reprint_set = rng.choice([s for s in _SET_CODES if s != set_code])
        pool.append(
            _card(
                f"reprint:{name}:{reprint_set}:{num_left}",
                name,
                number,
                reprint_set,
                hp=hp,
            )
        )
        if kind == "reprint_no_setcode":
            p_set = None  # set symbol didn't read → undecidable at top-1
            decidable = False

    elif kind == "noisy_name":
        # OCR mangled the name; the number still pins it.
        p_title = _noise_name(name, rng, errors=rng.choice([1, 2]))
        # Add a same-name-as-truth reprint sometimes to keep it honest.
        if rng.random() < 0.4:
            reprint_set = rng.choice([s for s in _SET_CODES if s != set_code])
            other_left = num_left + rng.choice([5, 20, 80])
            pool.append(
                _card(
                    f"nlook:{name}:{reprint_set}:{other_left}",
                    name,
                    f"{other_left}/{set_size}",
                    reprint_set,
                )
            )
            has_conflict = True

    elif kind == "name_only":
        # Vintage / no legible number: name is all we have. A same-name reprint
        # in another set makes it a top-2 scenario.
        p_number = None
        p_set = None
        reprint_set = rng.choice([s for s in _SET_CODES if s != set_code])
        pool.append(
            _card(f"reprint:{name}:{reprint_set}:{num_left}", name, number, reprint_set)
        )
        decidable = False

    pool.extend(_random_distractors(rng, name, rng.randint(2, 5)))
    rng.shuffle(pool)

    parsed = ParsedCard(
        title=p_title,
        title_candidates=[(p_title, 0.9)],
        set_code=p_set,
        card_number=p_number,
        hp=hp if rng.random() < 0.7 else None,
    )
    return Scenario(
        kind=kind,
        parsed=parsed,
        ocr_confidence=ocr_conf,
        pool=pool,
        truth_id=truth["id"],
        decidable=decidable,
        has_conflict_distractor=has_conflict,
    )


def generate(n: int, *, seed: int = 20260630) -> list[Scenario]:
    """A reproducible batch of ``n`` scenarios."""
    rng = random.Random(seed)
    return [make_scenario(rng) for _ in range(n)]


def rank(scenario: Scenario) -> list[tuple[dict[str, Any], float]]:
    """Score + sort a scenario's pool exactly as the pipeline does.

    Mirrors ``CardIdentifier``: confidence desc, ties broken by the canonical
    printing key so equal-score reprints order deterministically instead of by
    pool insertion order.
    """
    from app.services.identification.card_identifier import CardIdentifier

    scored = [
        (
            cand,
            score_candidate(
                parsed=scenario.parsed,
                candidate=cand,
                ocr_confidence=scenario.ocr_confidence,
                phash_hit=False,
            ).final,
            CardIdentifier._canonical_rank_key(cand),
        )
        for cand in scenario.pool
    ]
    scored.sort(key=lambda t: (-t[1], t[2]))
    return [(cand, final) for cand, final, _ in scored]


__all__ = ["Scenario", "generate", "make_scenario", "rank"]
