"""Canonical list of seeded test personas.

This module is the **single source of truth** for the demo accounts that
ship with every Loupe environment. It powers two consumers:

1. ``scripts/seed_test_users.py`` — reads this list to (re-)seed the
   database idempotently. Re-running the seeder never duplicates rows.
2. ``documentation/test_users_page.py`` — server-renders the
   ``/test-users`` HTML page that's linked from the Scalar API docs so
   any developer hitting ``/api-docs`` can see what's available.

Each persona is intentionally distinct along at least one of:
* vault size (0 — 500 graded cards)
* archetype / TCG focus
* grade distribution (low, mid, gem-mint only)
* scanner count and transport mix (none / 1 BLE / multi-transport / offline)
* tenure (brand new, normal, long-tenured)
* auth method (password, Apple-only, Google-only)

So end-to-end tests, demo videos, and screenshots can all reach for a
persona that *exactly* matches the scenario they need without writing
fixtures.

Every demo email follows the pattern ``test+NN@loupe.app`` and the
common password is ``Loupe2026!``. Personas tied to SSO have
``password=None`` and a synthetic Apple/Google subject instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DEFAULT_PASSWORD = "Loupe2026!"

# Catalog keys that exist in ``scripts/seed_test_users.CATALOG``. Personas
# whose ``archetype`` matches one of these draw cards from that single
# catalog. Personas with ``archetype="mixed"`` draw from all catalogs;
# ``archetype="grail"`` draws from ``magic_reserved`` only (whales).
Archetype = Literal[
    "empty",
    "vintage_pokemon",
    "modern_pokemon",
    "magic_reserved",
    "yugioh_meta",
    "sports_basketball",
    "sports_baseball",
    "onepiece",
    "lorcana",
    "mixed",
    "grail",
]

#: Auth method used to sign the persona in.
AuthMethod = Literal["password", "apple", "google"]

#: Health of the persona's scanners.
ScannerProfile = Literal[
    "none",          # 0 scanners
    "fresh",         # 1 scanner, paired today, never used
    "active",        # 1 scanner, last_seen recent
    "offline",       # 1 scanner, last_seen 30+ days ago
    "dual",          # 2 scanners (1 BLE active + 1 WiFi active)
    "multi",         # 3 scanners (BLE + WiFi + offline)
    "fleet",         # 5 scanners (mix of transports/states)
]


@dataclass(frozen=True)
class Persona:
    """A seeded demo account with deterministic content."""

    id: int                              # 1..50, used to build email + scanner ids
    name: str                            # display_name on the User row
    archetype: Archetype                 # which catalog to draw cards from
    vault_size: int                      # number of GradedCard rows to create
    avg_grade: float                     # target mean for the grade distribution
    scanner_profile: ScannerProfile      # number + state of scanners
    auth: AuthMethod                     # how the persona signs in
    tenure_days: int                     # created_at = now - tenure_days
    headline: str                        # one-sentence summary for the docs page
    why_unique: str                      # what makes this persona different
    tags: tuple[str, ...] = ()           # short labels for filtering / display

    @property
    def email(self) -> str:
        return f"test+{self.id:02d}@loupe.app"

    @property
    def password(self) -> str | None:
        return DEFAULT_PASSWORD if self.auth == "password" else None


# ─────────────────────────────────────────────────────────────────────────
# THE 50 PERSONAS
# ─────────────────────────────────────────────────────────────────────────
#
# Organised into six bands. Whenever you add a persona, keep the band it
# belongs to contiguous so the docs page renders a clean grouping.
#
PERSONAS: list[Persona] = [
    # ── Band 1 — Empty / first-run states (5) ──────────────────────────
    Persona(
        id=1, name="Ava Newbie", archetype="empty",
        vault_size=0, avg_grade=0.0, scanner_profile="none",
        auth="password", tenure_days=0,
        headline="Zero state — brand-new account, nothing scanned.",
        why_unique="Use for onboarding screenshots: every list is empty, "
                   "the Command Center shows the welcome state.",
        tags=("empty", "onboarding"),
    ),
    Persona(
        id=2, name="Ben Justpaired", archetype="empty",
        vault_size=0, avg_grade=0.0, scanner_profile="fresh",
        auth="password", tenure_days=0,
        headline="Paired a Loupe today, has not scanned anything yet.",
        why_unique="Tests the post-pair / pre-first-scan moment — "
                   "scanner widget shows the device but the vault is empty.",
        tags=("empty", "scanner-only"),
    ),
    Persona(
        id=3, name="Cleo Onecard", archetype="modern_pokemon",
        vault_size=1, avg_grade=9.0, scanner_profile="none",
        auth="password", tenure_days=2,
        headline="One scanned card, no scanner — pure phone-camera user.",
        why_unique="Smallest non-empty vault. Verifies single-row layouts "
                   "(history sparkline of one point, etc.).",
        tags=("minimal", "phone-only"),
    ),
    Persona(
        id=4, name="Dax Firstscan", archetype="vintage_pokemon",
        vault_size=1, avg_grade=8.5, scanner_profile="fresh",
        auth="password", tenure_days=0,
        headline="True first-scan flow: 1 scanner + 1 card, paired today.",
        why_unique="Perfect for the marketing demo of the very first end-to-end run.",
        tags=("minimal", "demo"),
    ),
    Persona(
        id=5, name="Eli Twodevs", archetype="empty",
        vault_size=0, avg_grade=0.0, scanner_profile="dual",
        auth="password", tenure_days=7,
        headline="Two scanners paired, but no scans yet.",
        why_unique="Stress-tests the device picker when no historical data exists.",
        tags=("empty", "multi-device"),
    ),

    # ── Band 2 — Beginners (10) — single TCG, small vault ──────────────
    Persona(
        id=6, name="Faye Genwun", archetype="vintage_pokemon",
        vault_size=3, avg_grade=7.0, scanner_profile="active",
        auth="password", tenure_days=21,
        headline="Three Gen-1 Pokémon, mostly low grades.",
        why_unique="Low-grade-leaning vault — the value chart skews cheap.",
        tags=("beginner", "pokemon"),
    ),
    Persona(
        id=7, name="Gus Modern", archetype="modern_pokemon",
        vault_size=5, avg_grade=9.0, scanner_profile="active",
        auth="password", tenure_days=30,
        headline="Five modern Pokémon ultra-rares, clean grades.",
        why_unique="Healthy starter portfolio with a tight grade band.",
        tags=("beginner", "pokemon"),
    ),
    Persona(
        id=8, name="Hana Magicstart", archetype="magic_reserved",
        vault_size=4, avg_grade=7.5, scanner_profile="active",
        auth="password", tenure_days=45,
        headline="Four Reserved-List Magic singles, mid grades.",
        why_unique="Small vault dominated by very high per-card value — "
                   "exercises currency formatting on big numbers.",
        tags=("beginner", "magic", "high-value"),
    ),
    Persona(
        id=9, name="Ivo Yugihigh", archetype="yugioh_meta",
        vault_size=6, avg_grade=8.5, scanner_profile="active",
        auth="password", tenure_days=60,
        headline="Six Yu-Gi-Oh LOB classics, mid-to-high grades.",
        why_unique="Non-Pokémon non-Magic for TCG filter coverage.",
        tags=("beginner", "yugioh"),
    ),
    Persona(
        id=10, name="Joon Rookiehunter", archetype="sports_basketball",
        vault_size=5, avg_grade=9.0, scanner_profile="active",
        auth="password", tenure_days=30,
        headline="Five NBA rookies, mostly PSA 9.",
        why_unique="Sports-only vault, great for testing sport-vs-TCG filters.",
        tags=("beginner", "sports"),
    ),
    Persona(
        id=11, name="Kai Diamonds", archetype="sports_baseball",
        vault_size=4, avg_grade=8.5, scanner_profile="active",
        auth="password", tenure_days=20,
        headline="Four MLB stars on Topps Chrome.",
        why_unique="Mid-grade baseball-only — counterpart to Joon for sport coverage.",
        tags=("beginner", "sports"),
    ),
    Persona(
        id=12, name="Lia Strawhat", archetype="onepiece",
        vault_size=5, avg_grade=9.0, scanner_profile="active",
        auth="password", tenure_days=40,
        headline="Five One Piece cards from Romance Dawn.",
        why_unique="Validates the ``onepiece`` TCG enum end-to-end.",
        tags=("beginner", "onepiece"),
    ),
    Persona(
        id=13, name="Mio Disneyfan", archetype="lorcana",
        vault_size=4, avg_grade=9.0, scanner_profile="active",
        auth="password", tenure_days=50,
        headline="Four Lorcana TFC cards graded by Loupe.",
        why_unique="100% first-party (loupe) graded vault — useful for "
                   "the 'house = loupe' badge UI.",
        tags=("beginner", "lorcana"),
    ),
    Persona(
        id=14, name="Nia Sampler", archetype="mixed",
        vault_size=6, avg_grade=8.5, scanner_profile="active",
        auth="password", tenure_days=15,
        headline="Six-card sampler across every TCG.",
        why_unique="One card per TCG — easiest way to screenshot a multi-TCG vault.",
        tags=("beginner", "mixed"),
    ),
    Persona(
        id=15, name="Owen Sportsmix", archetype="mixed",
        vault_size=8, avg_grade=8.7, scanner_profile="active",
        auth="password", tenure_days=25,
        headline="Mix of basketball + baseball cards.",
        why_unique="Sports-leaning mixed bag — exercises TCG=sports filters.",
        tags=("beginner", "sports", "mixed"),
    ),

    # ── Band 3 — Intermediate (10) — 10-30 cards, deeper history ───────
    Persona(
        id=16, name="Pia Holohunter", archetype="vintage_pokemon",
        vault_size=12, avg_grade=8.5, scanner_profile="active",
        auth="password", tenure_days=120,
        headline="Twelve vintage Pokémon holos — mid-tier collector.",
        why_unique="Original Band-3 reference vault; matches the legacy "
                   "``vintage_pokemon`` profile.",
        tags=("intermediate", "pokemon"),
    ),
    Persona(
        id=17, name="Quin Modernset", archetype="modern_pokemon",
        vault_size=18, avg_grade=9.2, scanner_profile="active",
        auth="password", tenure_days=90,
        headline="Eighteen Crown-Zenith cards, average PSA 9.2.",
        why_unique="High-throughput modern grader; lots of recent scan timestamps.",
        tags=("intermediate", "pokemon"),
    ),
    Persona(
        id=18, name="Rae Reserved", archetype="magic_reserved",
        vault_size=8, avg_grade=7.8, scanner_profile="active",
        auth="password", tenure_days=180,
        headline="Eight Magic Reserved-List singles.",
        why_unique="Highest per-card mean value of any persona — Black-Lotus territory.",
        tags=("intermediate", "magic", "high-value"),
    ),
    Persona(
        id=19, name="Sai Yugiveteran", archetype="yugioh_meta",
        vault_size=15, avg_grade=8.8, scanner_profile="active",
        auth="password", tenure_days=140,
        headline="Fifteen Yu-Gi-Oh LOB cards, PSA/CGC mix.",
        why_unique="Cross-house grade distribution; tests house-filter UI.",
        tags=("intermediate", "yugioh"),
    ),
    Persona(
        id=20, name="Tia Hoopshead", archetype="sports_basketball",
        vault_size=14, avg_grade=9.0, scanner_profile="active",
        auth="password", tenure_days=110,
        headline="Fourteen Prizm basketball cards.",
        why_unique="Sports vault with enough scans to make the history "
                   "chart non-degenerate.",
        tags=("intermediate", "sports"),
    ),
    Persona(
        id=21, name="Uri Mound", archetype="sports_baseball",
        vault_size=16, avg_grade=8.7, scanner_profile="dual",
        auth="password", tenure_days=130,
        headline="Sixteen baseball cards, two paired scanners.",
        why_unique="First multi-scanner persona in this band — tests "
                   "device-attribution in scan history.",
        tags=("intermediate", "sports", "multi-device"),
    ),
    Persona(
        id=22, name="Vex Cgcsfan", archetype="modern_pokemon",
        vault_size=20, avg_grade=9.4, scanner_profile="active",
        auth="password", tenure_days=160,
        headline="Twenty modern Pokémon, CGC-leaning.",
        why_unique="Exercises a third grading house (CGC) heavily; "
                   "default views skew PSA so this catches missed cases.",
        tags=("intermediate", "pokemon", "cgc"),
    ),
    Persona(
        id=23, name="Wyn Sealedplayer", archetype="onepiece",
        vault_size=10, avg_grade=9.3, scanner_profile="active",
        auth="password", tenure_days=70,
        headline="Ten One Piece leader cards, gem-mint.",
        why_unique="Highest concentration of PSA 10s in a small vault.",
        tags=("intermediate", "onepiece", "gem-mint"),
    ),
    Persona(
        id=24, name="Xan Disneyvault", archetype="lorcana",
        vault_size=9, avg_grade=9.1, scanner_profile="active",
        auth="password", tenure_days=85,
        headline="Nine Lorcana cards, Loupe-graded.",
        why_unique="Pure Loupe-house vault at intermediate size — "
                   "useful for first-party grading screenshots.",
        tags=("intermediate", "lorcana"),
    ),
    Persona(
        id=25, name="Yui Mixedfun", archetype="mixed",
        vault_size=22, avg_grade=8.9, scanner_profile="dual",
        auth="password", tenure_days=100,
        headline="Twenty-two cards across all TCGs, two devices.",
        why_unique="Best 'realistic average user' vault for marketing screenshots.",
        tags=("intermediate", "mixed"),
    ),

    # ── Band 4 — Power users (10) — 40-100 cards, multi-device ─────────
    Persona(
        id=26, name="Zane Grindmaster", archetype="vintage_pokemon",
        vault_size=60, avg_grade=8.6, scanner_profile="multi",
        auth="password", tenure_days=300,
        headline="Sixty vintage Pokémon, three scanners.",
        why_unique="Heavy vintage vault with a fleet of devices — "
                   "tests pagination on the vault list.",
        tags=("power", "pokemon", "multi-device"),
    ),
    Persona(
        id=27, name="Ada Modernpro", archetype="modern_pokemon",
        vault_size=80, avg_grade=9.3, scanner_profile="multi",
        auth="password", tenure_days=240,
        headline="Eighty modern Pokémon scans, three devices.",
        why_unique="Largest single-TCG modern vault.",
        tags=("power", "pokemon"),
    ),
    Persona(
        id=28, name="Bex Magicwhale", archetype="magic_reserved",
        vault_size=40, avg_grade=8.2, scanner_profile="multi",
        auth="password", tenure_days=400,
        headline="Forty Reserved-List singles — six-figure vault.",
        why_unique="Highest total value in this band — exercises the "
                   "'> $1M' edge case in currency formatting.",
        tags=("power", "magic", "high-value"),
    ),
    Persona(
        id=29, name="Cal Yugiarchive", archetype="yugioh_meta",
        vault_size=55, avg_grade=8.9, scanner_profile="multi",
        auth="password", tenure_days=260,
        headline="Fifty-five Yu-Gi-Oh LOB cards.",
        why_unique="Deepest Yu-Gi-Oh vault — used to test bulk-house mixes.",
        tags=("power", "yugioh"),
    ),
    Persona(
        id=30, name="Dax Hoopslord", archetype="sports_basketball",
        vault_size=65, avg_grade=9.1, scanner_profile="multi",
        auth="password", tenure_days=210,
        headline="Sixty-five NBA cards across multiple scanners.",
        why_unique="Power-user sports vault with three devices.",
        tags=("power", "sports", "multi-device"),
    ),
    Persona(
        id=31, name="Eve Diamondking", archetype="sports_baseball",
        vault_size=70, avg_grade=8.8, scanner_profile="multi",
        auth="password", tenure_days=320,
        headline="Seventy baseball cards.",
        why_unique="Largest baseball-only vault.",
        tags=("power", "sports"),
    ),
    Persona(
        id=32, name="Finn Onepiecepro", archetype="onepiece",
        vault_size=45, avg_grade=9.4, scanner_profile="multi",
        auth="password", tenure_days=180,
        headline="Forty-five One Piece cards, gem-mint heavy.",
        why_unique="Power-user vault on an enum the legacy seeder under-covered.",
        tags=("power", "onepiece", "gem-mint"),
    ),
    Persona(
        id=33, name="Gia Castlecourt", archetype="lorcana",
        vault_size=50, avg_grade=9.2, scanner_profile="multi",
        auth="password", tenure_days=190,
        headline="Fifty Lorcana cards across two sets.",
        why_unique="Power-user Lorcana — useful for first-party grade ranking demos.",
        tags=("power", "lorcana"),
    ),
    Persona(
        id=34, name="Hugo Omnigrader", archetype="mixed",
        vault_size=100, avg_grade=9.0, scanner_profile="multi",
        auth="password", tenure_days=365,
        headline="One hundred cards, every TCG, every house.",
        why_unique="The widest distribution in the seed — every filter is exercised.",
        tags=("power", "mixed"),
    ),
    Persona(
        id=35, name="Ira Fleetowner", archetype="mixed",
        vault_size=75, avg_grade=9.0, scanner_profile="fleet",
        auth="password", tenure_days=280,
        headline="Five paired scanners (BLE + WiFi mix), 75 cards.",
        why_unique="First persona with 5 scanners — tests scanner-list pagination.",
        tags=("power", "mixed", "fleet"),
    ),

    # ── Band 5 — Whales (5) — 200-500 cards, full fleets ───────────────
    Persona(
        id=36, name="Jett Vintagebaron", archetype="vintage_pokemon",
        vault_size=200, avg_grade=8.7, scanner_profile="fleet",
        auth="password", tenure_days=730,
        headline="Two hundred vintage Pokémon, five-scanner shop.",
        why_unique="Whale-tier; useful for load-testing list endpoints.",
        tags=("whale", "pokemon"),
    ),
    Persona(
        id=37, name="Kit Modernshop", archetype="modern_pokemon",
        vault_size=300, avg_grade=9.3, scanner_profile="fleet",
        auth="password", tenure_days=500,
        headline="Three hundred modern Pokémon, five scanners.",
        why_unique="Largest single-TCG vault; pagination + summary aggregates.",
        tags=("whale", "pokemon"),
    ),
    Persona(
        id=38, name="Liv Reservedqueen", archetype="magic_reserved",
        vault_size=120, avg_grade=8.5, scanner_profile="fleet",
        auth="password", tenure_days=900,
        headline="A hundred-twenty Reserved List singles — seven-figure vault.",
        why_unique="Highest absolute USD value of any persona.",
        tags=("whale", "magic", "high-value"),
    ),
    Persona(
        id=39, name="Mox Allcards", archetype="mixed",
        vault_size=500, avg_grade=8.9, scanner_profile="fleet",
        auth="password", tenure_days=1000,
        headline="Five hundred cards across all TCGs — the stress-test account.",
        why_unique="Biggest vault in the seed. Use to validate the "
                   "vault list, history aggregation, and summary endpoints "
                   "perform under load.",
        tags=("whale", "mixed", "stress"),
    ),
    Persona(
        id=40, name="Nyx Sportsempire", archetype="mixed",
        vault_size=350, avg_grade=9.0, scanner_profile="fleet",
        auth="password", tenure_days=800,
        headline="Three-fifty mixed sports cards, five scanners.",
        why_unique="Sports-heavy whale; complements Mox for non-TCG load testing.",
        tags=("whale", "sports"),
    ),

    # ── Band 6 — Edge cases & special states (10) ──────────────────────
    Persona(
        id=41, name="Oz Grailonly", archetype="grail",
        vault_size=1, avg_grade=10.0, scanner_profile="active",
        auth="password", tenure_days=400,
        headline="Single Black Lotus PSA 10 — the grail holder.",
        why_unique="Tests the 'single ultra-high-value card' UI — sparklines, "
                   "summary doughnut, and Top Movers all become 100% one card.",
        tags=("edge", "grail", "high-value"),
    ),
    Persona(
        id=42, name="Pax Allgemmint", archetype="modern_pokemon",
        vault_size=25, avg_grade=10.0, scanner_profile="active",
        auth="password", tenure_days=180,
        headline="Twenty-five cards, every single one PSA 10.",
        why_unique="Zero variance in grade — tests grade-distribution charts "
                   "when there's only one bucket.",
        tags=("edge", "gem-mint"),
    ),
    Persona(
        id=43, name="Qua Lowgrades", archetype="vintage_pokemon",
        vault_size=20, avg_grade=4.0, scanner_profile="active",
        auth="password", tenure_days=180,
        headline="Twenty vintage cards, all PSA 3-5.",
        why_unique="Opposite of Pax — all low grades. Catches off-by-one "
                   "bugs in 'PSA 9+' filter logic.",
        tags=("edge", "low-grade"),
    ),
    Persona(
        id=44, name="Rio Apple", archetype="modern_pokemon",
        vault_size=12, avg_grade=9.0, scanner_profile="active",
        auth="apple", tenure_days=90,
        headline="Sign-in with Apple, no password.",
        why_unique="Validates ``apple_subject`` auth path end-to-end.",
        tags=("edge", "auth", "apple"),
    ),
    Persona(
        id=45, name="Sky Google", archetype="modern_pokemon",
        vault_size=12, avg_grade=9.0, scanner_profile="active",
        auth="google", tenure_days=90,
        headline="Sign-in with Google, no password.",
        why_unique="Validates ``google_subject`` auth path end-to-end.",
        tags=("edge", "auth", "google"),
    ),
    Persona(
        id=46, name="Tor Offlinedev", archetype="modern_pokemon",
        vault_size=10, avg_grade=9.0, scanner_profile="offline",
        auth="password", tenure_days=120,
        headline="One scanner, last seen 30 days ago.",
        why_unique="Hardware widget should render the 'stale device' badge.",
        tags=("edge", "offline"),
    ),
    Persona(
        id=47, name="Uma Multitrans", archetype="modern_pokemon",
        vault_size=15, avg_grade=9.0, scanner_profile="multi",
        auth="password", tenure_days=150,
        headline="One BLE + one WiFi + one offline scanner.",
        why_unique="Every ``ScannerTransportEnum`` value is represented "
                   "on one user simultaneously.",
        tags=("edge", "multi-device", "transport-mix"),
    ),
    Persona(
        id=48, name="Vic Newaccount", archetype="modern_pokemon",
        vault_size=4, avg_grade=9.0, scanner_profile="fresh",
        auth="password", tenure_days=0,
        headline="Account + scanner created today, 4 scans in last hour.",
        why_unique="All timestamps within the last day — exercises the "
                   "'today' rollup on the activity feed.",
        tags=("edge", "fresh"),
    ),
    Persona(
        id=49, name="Wes Veteran", archetype="mixed",
        vault_size=30, avg_grade=8.8, scanner_profile="active",
        auth="password", tenure_days=1500,
        headline="Account is 4+ years old, steady moderate activity.",
        why_unique="Longest tenure in the seed — useful for 'member since' "
                   "and lifetime-stats endpoints.",
        tags=("edge", "tenure"),
    ),
    Persona(
        id=50, name="Zed Burstuser", archetype="mixed",
        vault_size=40, avg_grade=9.0, scanner_profile="dual",
        auth="password", tenure_days=3,
        headline="Created 3 days ago, already scanned 40 cards.",
        why_unique="Highest scan-velocity persona — tests rate-limit "
                   "and bulk-write paths.",
        tags=("edge", "high-velocity"),
    ),
]

# Sanity invariant — every persona id must be unique and 1..N contiguous.
assert [p.id for p in PERSONAS] == list(range(1, len(PERSONAS) + 1))


# Useful band groupings for the docs page UI.
BANDS: list[tuple[str, str, range]] = [
    ("Empty / first-run", "Brand-new accounts, scanner-only, or one-card vaults.", range(1, 6)),
    ("Beginners", "Single-TCG vaults with 3 – 8 cards.", range(6, 16)),
    ("Intermediate", "10 – 30 cards, deeper history, sometimes a second device.", range(16, 26)),
    ("Power users", "40 – 100 cards across 2 – 3 paired scanners.", range(26, 36)),
    ("Whales", "200 – 500 cards, full 5-scanner fleets.", range(36, 41)),
    ("Edge cases", "All-gem-mint, all-low-grade, SSO, stale scanners, etc.", range(41, 51)),
]


__all__ = [
    "Archetype",
    "AuthMethod",
    "BANDS",
    "DEFAULT_PASSWORD",
    "PERSONAS",
    "Persona",
    "ScannerProfile",
]
