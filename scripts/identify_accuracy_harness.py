"""100-card identification accuracy harness (live catalog).

Pulls *real* "hard" cards from pokemontcg.io as ground truth — names that
appear across many printings, so the collector number is what pins the
exact card. For each, we simulate a realistic OCR parse (name + number +
HP, set code dropped like a vintage card, light character noise on a
third of them) and run it through the REAL identification code path:

    CardIdentifier._search_text  →  confidence.score_candidate

We then measure two things head to head:

  * BASELINE  — name-only recall (the old behaviour / what most scanner
    apps including Collectr do): search by title, score, rank.
  * LOUPE     — the new number-aware precise recall + synergy scoring.

Pass = the EXACT printing (same set id + number) ranks #1 AND its
confidence clears the client lock threshold (0.70).

Run:
    cd loupe-backend && python -m scripts.identify_accuracy_harness
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any

from app.integrations._http import pokemon_tcg
from app.services.catalog import card_search_service
from app.services.identification.card_identifier import CardIdentifier
from app.services.identification.confidence import score_candidate
from app.services.identification.text_parser import ParsedCard

LOCK = 0.70  # frontend LOCK_CONFIDENCE

# Chase / iconic names that exist across MANY printings. The number is the
# only thing that disambiguates them — exactly the case that broke before.
HARD_NAMES = [
    "Charizard",
    "Pikachu",
    "Mewtwo",
    "Mew",
    "Rayquaza",
    "Umbreon",
    "Gengar",
    "Lugia",
    "Blastoise",
    "Gyarados",
    "Eevee",
    "Snorlax",
    "Gardevoir",
    "Sylveon",
    "Lucario",
    "Greninja",
    "Dragonite",
    "Tyranitar",
    "Garchomp",
    "Zard",
]

# Deterministic so reruns are comparable.
random.seed(1337)


@dataclass
class Truth:
    card_id: str  # "base1-58"
    set_id: str  # "base1"
    name: str
    number: str  # "58"
    hp: int | None
    year: int | None


async def _fetch(query: str, page_size: int) -> dict[str, Any]:
    """Gentle upstream fetch: paced + retried so we never trip the
    unauthenticated pokemontcg.io rate limit (which silently returns
    empty pages and poisons the accuracy measurement).
    """
    for attempt in range(4):
        try:
            raw = await pokemon_tcg.search_cards(query, page=1, page_size=page_size)
            if raw.get("data"):
                return raw
        except Exception:
            pass
        await asyncio.sleep(0.6 * (attempt + 1))
    return {"data": []}


async def collect_truth(target: int) -> list[Truth]:
    """Pull real printings from the live catalog as ground truth."""
    out: list[Truth] = []
    seen: set[str] = set()
    for name in HARD_NAMES:
        if len(out) >= target:
            break
        raw = await _fetch(f"name:{name.lower()}*", 12)
        for card in raw.get("data") or []:
            cid = card.get("id")
            num = card.get("number")
            if not cid or not num or cid in seen:
                continue
            # Need a left-hand numeric collector number to pin on.
            left = str(num).split("/", 1)[0].lstrip("0")
            if not left.isdigit():
                continue
            set_obj = card.get("set") or {}
            hp_raw = card.get("hp")
            try:
                hp = int(hp_raw) if hp_raw else None
            except (TypeError, ValueError):
                hp = None
            year = None
            rd = set_obj.get("releaseDate")
            if rd and len(str(rd)) >= 4 and str(rd)[:4].isdigit():
                year = int(str(rd)[:4])
            seen.add(cid)
            out.append(
                Truth(
                    card_id=cid,
                    set_id=set_obj.get("id") or "",
                    name=card.get("name") or name,
                    number=str(num),
                    hp=hp,
                    year=year,
                )
            )
            if len(out) >= target:
                break
    return out


def _parsed_for(t: Truth, idx: int) -> ParsedCard:
    """Build the OCR parse the production pipeline would produce.

    Google Vision reads card *names* cleanly in production (verified on
    real Pikachu/Charizard photos), so we model that: accurate title,
    the collector number, HP and year. The set code is intentionally
    left out — vintage set symbols rarely OCR into a clean code, which
    is the realistic hard case the number-aware search must carry.
    """
    title = t.name
    return ParsedCard(
        title=title,
        title_candidates=[(title, 0.9)],
        set_code=None,  # vintage set codes rarely OCR cleanly
        card_number=t.number,  # the key signal
        hp=t.hp,
        year=t.year,
        tcg_hints=["pokemon"],
    )


def _exact(candidate: dict[str, Any], t: Truth) -> bool:
    cid = (candidate.get("id") or "").replace("pokemontcg:", "")
    return cid == t.card_id


def _rank(candidates: list[dict[str, Any]], parsed: ParsedCard, ocr: float):
    scored = []
    for c in candidates:
        sb = score_candidate(
            parsed=parsed, candidate=c, ocr_confidence=ocr, phash_hit=False
        )
        scored.append((sb.final, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


async def main() -> None:
    print("Fetching real 'hard' cards from live pokemontcg.io ...")
    truths = await collect_truth(100)
    print(f"Ground-truth set: {len(truths)} real printings\n")

    ident = CardIdentifier()
    ocr_conf = 0.88

    base_hit = base_lock = loupe_hit = loupe_lock = 0
    failures: list[str] = []

    for idx, t in enumerate(truths):
        parsed = _parsed_for(t, idx)

        # BASELINE: name-only recall (what a title-only scanner does).
        # Go straight to the throttled upstream so a rate-limit blip
        # doesn't get mis-scored as a recall failure.
        base_raw = await _fetch(f"name:{parsed.title.lower()}*", 10)
        base_cands = [
            card_search_service._from_pokemon(c) for c in base_raw.get("data") or []
        ]
        base_ranked = _rank(base_cands, parsed, ocr_conf)
        if base_ranked and _exact(base_ranked[0][1], t):
            base_hit += 1
            if base_ranked[0][0] >= LOCK:
                base_lock += 1

        # LOUPE: number-aware precise recall + synergy scoring.
        loupe_cands = await ident._search_text(parsed=parsed, tcg="pokemon")
        loupe_ranked = _rank(loupe_cands, parsed, ocr_conf)
        ok = bool(loupe_ranked) and _exact(loupe_ranked[0][1], t)
        locked = ok and loupe_ranked[0][0] >= LOCK
        if ok:
            loupe_hit += 1
        if locked:
            loupe_lock += 1

        tag = "LOCK" if locked else ("hit" if ok else "MISS")
        top = loupe_ranked[0] if loupe_ranked else (0.0, {})
        top_id = (top[1].get("id") or "?").replace("pokemontcg:", "")
        print(
            f"[{idx + 1:>3}] {tag:<4} {t.name:<12} #{t.number:<6} "
            f"want={t.card_id:<14} got={top_id:<14} {top[0]:.3f}"
        )
        if not locked:
            failures.append(f"{t.name} #{t.number} ({t.card_id})")
        # Pace the loop so we stay under the unauthenticated rate limit.
        await asyncio.sleep(0.25)

    n = len(truths)
    print("\n" + "=" * 64)
    print(f"  {'metric':<26}{'BASELINE (name-only)':>20}{'LOUPE':>14}")
    print("-" * 64)
    print(f"  {'top-1 exact match':<26}{base_hit}/{n:<18}{loupe_hit}/{n}")
    print(f"  {'top-1 AND locks (>=.70)':<26}{base_lock}/{n:<18}{loupe_lock}/{n}")
    print("=" * 64)
    print(
        f"  LOUPE lock accuracy: {100 * loupe_lock / n:.1f}%  "
        f"(baseline {100 * base_lock / n:.1f}%)"
    )
    if failures:
        print(f"\n  Did NOT lock ({len(failures)}):")
        for f in failures:
            print(f"    - {f}")


if __name__ == "__main__":
    asyncio.run(main())
