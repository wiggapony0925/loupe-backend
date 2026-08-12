"""Router tests for admin catalog coverage + mirror ops (`/v1/admin/catalog`).

Two different things share this prefix. The coverage summary is a read-only
roll-up of how much data backs each game and how much of it the scanner can
match on. The mirror endpoints are the manual controls for the local copy of
the Pokémon / Magic / Yu-Gi-Oh catalogs — long, expensive, upstream-facing
jobs, which is exactly why the tests here assert that the endpoint *dispatches*
with the bounds it was given rather than letting a real sync run.

Every function in the mirror service that would leave the process is stubbed
for the whole module (see `no_upstream_calls`), so a mistake in one test can't
turn into a live Scryfall/YGOPRODeck download.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.card import Card, CardSet
from app.models.catalog_mirror import CatalogMirrorCard, CatalogMirrorSet
from app.models.enums import GradeHouseEnum, PriceSourceEnum, TcgEnum
from app.models.price import PriceSnapshot
from app.services.catalog import pokemon_mirror_service
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


@pytest.fixture
async def admin_user(db_session):
    """A Loupe staff account — the developer portal's caller."""
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    from app.auth.jwt import issue_token

    token, _ = issue_token(admin_user.id, "access")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def no_upstream_calls(monkeypatch):
    """Replace every mirror function that talks to an upstream provider.

    Returns the call log so a test can assert what the endpoint dispatched.
    The DB-only helpers (`mirror_status`, `stale_price_set_ids`) are left real
    — they are cheap, and the assertions are more truthful for it.
    """
    calls: dict[str, list] = {
        "pokemon": [],
        "magic": [],
        "yugioh": [],
        "refresh": [],
    }

    async def fake_pokemon(*, force: bool = False, max_sets: int | None = None):
        calls["pokemon"].append({"force": force, "max_sets": max_sets})
        return {"sets_synced": 3, "cards_synced": 120}

    async def fake_magic(*, max_cards: int | None = None):
        calls["magic"].append({"max_cards": max_cards})
        return {"tcg": "magic", "cards_total": 9, "cards_synced": 9}

    async def fake_yugioh(*, max_cards: int | None = None):
        calls["yugioh"].append({"max_cards": max_cards})
        return {"tcg": "yugioh", "cards_total": 7, "cards_synced": 7}

    async def fake_refresh(set_id: str) -> int:
        calls["refresh"].append(set_id)
        return 42

    monkeypatch.setattr(pokemon_mirror_service, "sync_pokemon_from_dump", fake_pokemon)
    monkeypatch.setattr(pokemon_mirror_service, "sync_magic_from_bulk", fake_magic)
    monkeypatch.setattr(pokemon_mirror_service, "sync_yugioh_from_dump", fake_yugioh)
    monkeypatch.setattr(pokemon_mirror_service, "refresh_set_prices", fake_refresh)
    return calls


async def _mirror_set(db, set_id: str, *, prices_synced_at: datetime | None) -> None:
    db.add(
        CatalogMirrorSet(
            id=set_id,
            source=pokemon_mirror_service.SOURCE,
            tcg=pokemon_mirror_service.TCG,
            name=f"Set {set_id}",
            prices_synced_at=prices_synced_at,
        )
    )
    await db.commit()


async def _mirror_card(db, card_id: str, *, sort_price: float | None) -> None:
    db.add(
        CatalogMirrorCard(
            id=card_id,
            source=pokemon_mirror_service.SOURCE,
            tcg=pokemon_mirror_service.TCG,
            upstream_id=card_id,
            set_id="base1",
            name="Charizard",
            name_lower="charizard",
            sort_price=sort_price,
            payload={"id": card_id},
        )
    )
    await db.commit()


# ── authorization ──


@pytest.mark.asyncio
async def test_catalog_coverage_is_not_readable_anonymously(client):
    assert_envelope_error(await client.get("/v1/admin/catalog"), expected_status=401)


@pytest.mark.asyncio
async def test_an_ordinary_user_cannot_read_catalog_coverage(client, auth_headers):
    for path in ("/v1/admin/catalog", "/v1/admin/catalog/mirror"):
        assert_envelope_error(
            await client.get(path, headers=auth_headers), expected_status=403
        )


@pytest.mark.asyncio
async def test_an_ordinary_user_cannot_start_a_mirror_sync(
    client, auth_headers, no_upstream_calls
):
    """A sync pulls whole catalogs from upstream and rewrites the mirror rows
    every search reads — the most expensive button in the portal."""
    responses = [
        await client.post("/v1/admin/catalog/mirror/sync", headers=auth_headers),
        await client.post(
            "/v1/admin/catalog/mirror/sync-tcg?tcg=magic", headers=auth_headers
        ),
        await client.post(
            "/v1/admin/catalog/mirror/refresh-prices", headers=auth_headers
        ),
    ]
    for resp in responses:
        assert_envelope_error(resp, expected_status=403)

    # The gate runs before the handler, so nothing was dispatched upstream.
    assert no_upstream_calls == {
        "pokemon": [],
        "magic": [],
        "yugioh": [],
        "refresh": [],
    }


@pytest.mark.asyncio
async def test_starting_a_mirror_sync_anonymously_is_refused(client):
    assert_envelope_error(
        await client.post("/v1/admin/catalog/mirror/sync"), expected_status=401
    )


# ── coverage summary ──


@pytest.mark.asyncio
async def test_coverage_reports_every_game_even_the_ones_with_no_data(
    client, admin_headers
):
    """The portal uses this page to decide what to build next, so a game that
    is marketed but unbacked has to appear with `backed=false` — not vanish."""
    body = assert_envelope_ok(
        await client.get("/v1/admin/catalog", headers=admin_headers)
    )

    assert {g["tcg"] for g in body["games"]} == {t.value for t in TcgEnum}
    assert all(g["backed"] is False for g in body["games"])
    assert body["total_cards"] == 0
    # No cards means no division — the percentage is 0, never a crash.
    assert body["phash_coverage_pct"] == 0.0


@pytest.mark.asyncio
async def test_coverage_counts_scanner_ready_cards_separately(
    client, admin_headers, db_session
):
    """`phash_pct` is the scanner's readiness number: a card without a
    perceptual hash can't be matched from a photo, however complete its row is.
    """
    cset = CardSet(tcg=TcgEnum.pokemon, name="Base", code="BS")
    db_session.add(cset)
    await db_session.flush()
    db_session.add_all(
        [
            Card(
                set_id=cset.id, tcg=TcgEnum.pokemon, name="Hashed", image_phash="a" * 64
            ),
            Card(set_id=cset.id, tcg=TcgEnum.pokemon, name="Unhashed"),
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/admin/catalog", headers=admin_headers)
    )

    pokemon = next(g for g in body["games"] if g["tcg"] == "pokemon")
    assert pokemon["cards"] == 2
    assert pokemon["sets"] == 1
    assert pokemon["phash_cards"] == 1
    assert pokemon["phash_pct"] == 0.5
    assert pokemon["backed"] is True
    assert body["total_cards"] == 2
    assert body["phash_coverage_pct"] == 0.5


@pytest.mark.asyncio
async def test_the_best_backed_game_is_listed_first(client, admin_headers, db_session):
    """Sorted by card count so the games with real catalogs stay at the top and
    the scaffolded ones sink, rather than the list being enum-ordered."""
    pkmn_set = CardSet(tcg=TcgEnum.pokemon, name="Base", code="BS")
    magic_set = CardSet(tcg=TcgEnum.magic, name="Alpha", code="LEA")
    db_session.add_all([pkmn_set, magic_set])
    await db_session.flush()
    db_session.add_all(
        [Card(set_id=magic_set.id, tcg=TcgEnum.magic, name=f"M{i}") for i in range(3)]
        + [Card(set_id=pkmn_set.id, tcg=TcgEnum.pokemon, name="P1")]
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/admin/catalog", headers=admin_headers)
    )

    assert body["games"][0]["tcg"] == "magic"
    assert body["games"][1]["tcg"] == "pokemon"
    assert body["total_sets"] == 2


@pytest.mark.asyncio
async def test_coverage_breaks_price_snapshots_down_by_source(
    client, admin_headers, db_session
):
    """Which provider the prices came from is the answer to "can we drop this
    integration?", so the roll-up keeps the per-source split, not just a total.
    """
    cset = CardSet(tcg=TcgEnum.pokemon, name="Base", code="BS")
    db_session.add(cset)
    await db_session.flush()
    card = Card(set_id=cset.id, tcg=TcgEnum.pokemon, name="Charizard")
    db_session.add(card)
    await db_session.flush()
    db_session.add_all(
        [
            PriceSnapshot(
                card_id=card.id,
                house=GradeHouseEnum.psa,
                grade=Decimal("10.0"),
                source=source,
                price_usd=Decimal("100.00"),
            )
            for source in (
                PriceSourceEnum.ebay,
                PriceSourceEnum.ebay,
                PriceSourceEnum.tcgplayer,
            )
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/admin/catalog", headers=admin_headers)
    )

    assert body["price_snapshots"] == 3
    assert body["price_by_source"] == {"ebay": 2, "tcgplayer": 1}


# ── mirror status ──


@pytest.mark.asyncio
async def test_an_empty_mirror_reports_itself_as_not_ready(client, admin_headers):
    """`ready` is what the search path consults before trusting the mirror; an
    empty (or barely-synced) mirror must say no so lookups fall back to live."""
    body = assert_envelope_ok(
        await client.get("/v1/admin/catalog/mirror", headers=admin_headers)
    )
    assert body == {
        "ready": False,
        "cards": 0,
        "cards_priced": 0,
        "sets": 0,
        "sets_with_stale_prices": 0,
    }


@pytest.mark.asyncio
async def test_mirror_status_counts_priced_cards_and_stale_sets(
    client, admin_headers, db_session
):
    """The two numbers an admin acts on: how much of the mirror carries a price
    at all, and how many sets are overdue for a price pass."""
    await _mirror_card(db_session, "base1-1", sort_price=12.5)
    await _mirror_card(db_session, "base1-2", sort_price=None)
    await _mirror_set(db_session, "fresh", prices_synced_at=datetime.now(UTC))
    await _mirror_set(
        db_session, "stale", prices_synced_at=datetime.now(UTC) - timedelta(days=3)
    )
    await _mirror_set(db_session, "never", prices_synced_at=None)

    body = assert_envelope_ok(
        await client.get("/v1/admin/catalog/mirror", headers=admin_headers)
    )
    assert body["cards"] == 2
    assert body["cards_priced"] == 1
    assert body["sets"] == 3
    # Never-priced counts as stale; only the set refreshed today is exempt.
    assert body["sets_with_stale_prices"] == 2


# ── pokémon sync ──


@pytest.mark.asyncio
async def test_a_sync_dispatches_with_the_bounds_the_caller_asked_for(
    client, admin_headers, no_upstream_calls
):
    """A full sync can't finish inside one request, so the caller chunks it.
    Dropping `max_sets` on the floor would turn a chunked call into a timeout.
    """
    body = assert_envelope_ok(
        await client.post(
            "/v1/admin/catalog/mirror/sync?force=true&max_sets=5", headers=admin_headers
        )
    )

    assert no_upstream_calls["pokemon"] == [{"force": True, "max_sets": 5}]
    # The sync's own counters come back merged with a fresh status block, so
    # the portal can show progress without a second round-trip.
    assert body["sets_synced"] == 3
    assert body["status"]["ready"] is False


@pytest.mark.asyncio
async def test_a_sync_defaults_to_one_chunk_of_forty_sets(
    client, admin_headers, no_upstream_calls
):
    """The default is deliberately a chunk, not everything: an unbounded sync
    from the portal's default button press would exceed the request timeout."""
    assert_envelope_ok(
        await client.post("/v1/admin/catalog/mirror/sync", headers=admin_headers)
    )
    assert no_upstream_calls["pokemon"] == [{"force": False, "max_sets": 40}]


@pytest.mark.asyncio
async def test_asking_for_zero_sets_means_sync_everything_remaining(
    client, admin_headers, no_upstream_calls
):
    """0 is the documented escape hatch for "no cap" — it reaches the service
    as `None`, not as a request for zero sets."""
    assert_envelope_ok(
        await client.post(
            "/v1/admin/catalog/mirror/sync?max_sets=0", headers=admin_headers
        )
    )
    assert no_upstream_calls["pokemon"] == [{"force": False, "max_sets": None}]


@pytest.mark.asyncio
async def test_a_sync_chunk_larger_than_the_ceiling_is_rejected(
    client, admin_headers, no_upstream_calls
):
    resp = await client.post(
        "/v1/admin/catalog/mirror/sync?max_sets=501", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=422)
    assert no_upstream_calls["pokemon"] == []


# ── magic / yu-gi-oh sync ──


@pytest.mark.asyncio
async def test_each_tcg_is_routed_to_its_own_upstream(
    client, admin_headers, no_upstream_calls
):
    """One endpoint, two entirely different bulk sources (Scryfall vs
    YGOPRODeck). Crossing the wires would silently mirror the wrong catalog."""
    magic = assert_envelope_ok(
        await client.post(
            "/v1/admin/catalog/mirror/sync-tcg?tcg=magic", headers=admin_headers
        )
    )
    yugioh = assert_envelope_ok(
        await client.post(
            "/v1/admin/catalog/mirror/sync-tcg?tcg=yugioh", headers=admin_headers
        )
    )

    assert no_upstream_calls["magic"] == [{"max_cards": None}]
    assert no_upstream_calls["yugioh"] == [{"max_cards": None}]
    assert magic["tcg"] == "magic"
    assert yugioh["tcg"] == "yugioh"
    # Both report readiness for the game just synced.
    assert magic["ready"] is False
    assert yugioh["ready"] is False


@pytest.mark.asyncio
async def test_a_card_cap_is_forwarded_to_the_bulk_sync(
    client, admin_headers, no_upstream_calls
):
    assert_envelope_ok(
        await client.post(
            "/v1/admin/catalog/mirror/sync-tcg?tcg=magic&max_cards=25",
            headers=admin_headers,
        )
    )
    assert no_upstream_calls["magic"] == [{"max_cards": 25}]


@pytest.mark.asyncio
async def test_a_game_without_a_bulk_source_cannot_be_synced_here(
    client, admin_headers, no_upstream_calls
):
    """Only Magic and Yu-Gi-Oh ship a single-file catalog. Pokémon has its own
    chunked endpoint, so asking for it here is a client error, not a fallback.
    """
    resp = await client.post(
        "/v1/admin/catalog/mirror/sync-tcg?tcg=pokemon", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=422)
    assert no_upstream_calls["magic"] == []
    assert no_upstream_calls["yugioh"] == []


@pytest.mark.asyncio
async def test_the_tcg_must_be_named_explicitly(client, admin_headers):
    """No default: a sync-everything interpretation of a missing parameter
    would pull two full catalogs from a mis-typed URL."""
    resp = await client.post("/v1/admin/catalog/mirror/sync-tcg", headers=admin_headers)
    assert_envelope_error(resp, expected_status=422)


# ── price refresh ──


@pytest.mark.asyncio
async def test_the_price_walker_spends_its_budget_oldest_first(
    client, admin_headers, db_session, no_upstream_calls
):
    """Prices go stale set-by-set and the walker only gets a few sets per call,
    so it must spend them on the ones that have waited longest rather than on
    whatever order the DB happens to return. A set refreshed today is not
    stale at all and is skipped entirely."""
    now = datetime.now(UTC)
    await _mirror_set(db_session, "fresh", prices_synced_at=now)
    await _mirror_set(db_session, "older", prices_synced_at=now - timedelta(days=9))
    await _mirror_set(db_session, "newer", prices_synced_at=now - timedelta(days=2))

    body = assert_envelope_ok(
        await client.post(
            "/v1/admin/catalog/mirror/refresh-prices?sets=2", headers=admin_headers
        )
    )

    assert no_upstream_calls["refresh"] == ["older", "newer"]
    # Per-set counts come back so the admin sees what actually moved.
    assert body["refreshed"] == {"older": 42, "newer": 42}
    assert body["status"]["sets"] == 3


@pytest.mark.asyncio
async def test_a_never_priced_set_is_refreshed_before_merely_stale_ones(
    client, admin_headers, db_session, no_upstream_calls
):
    """A set that has never had a price is infinitely stale and goes first.

    Those are the sets showing users no price at all, so they outrank sets
    whose prices are merely a day old. The walker is budgeted per call, so the
    ordering decides what actually runs with the budget it has.
    """
    now = datetime.now(UTC)
    await _mirror_set(db_session, "never", prices_synced_at=None)
    await _mirror_set(db_session, "day_old", prices_synced_at=now - timedelta(days=1.5))

    assert_envelope_ok(
        await client.post(
            "/v1/admin/catalog/mirror/refresh-prices?sets=1", headers=admin_headers
        )
    )

    assert no_upstream_calls["refresh"] == ["never"]


@pytest.mark.asyncio
async def test_refreshing_prices_with_nothing_stale_is_a_no_op(
    client, admin_headers, db_session, no_upstream_calls
):
    """The portal polls this button; a mirror that is already fresh must cost
    nothing upstream instead of re-pulling the same sets."""
    await _mirror_set(db_session, "fresh", prices_synced_at=datetime.now(UTC))

    body = assert_envelope_ok(
        await client.post(
            "/v1/admin/catalog/mirror/refresh-prices", headers=admin_headers
        )
    )
    assert body["refreshed"] == {}
    assert no_upstream_calls["refresh"] == []


@pytest.mark.asyncio
async def test_the_price_walker_refuses_an_unbounded_batch(
    client, admin_headers, no_upstream_calls
):
    """Each set is a live API call made inline, so the batch size is bounded at
    both ends — 0 would be pointless and 61 would blow the request timeout."""
    for query in ("sets=0", "sets=61"):
        assert_envelope_error(
            await client.post(
                f"/v1/admin/catalog/mirror/refresh-prices?{query}",
                headers=admin_headers,
            ),
            expected_status=422,
        )
    assert no_upstream_calls["refresh"] == []


@pytest.mark.asyncio
async def test_only_the_pokemon_mirror_is_walked_for_prices(
    client, admin_headers, db_session, no_upstream_calls
):
    """Magic and Yu-Gi-Oh ship prices inside their bulk dumps, so their sets
    have no separate price pass and must not be picked up by this walker."""
    db_session.add(
        CatalogMirrorSet(
            id="lea",
            source="scryfall",
            tcg="magic",
            name="Alpha",
            prices_synced_at=None,
        )
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.post(
            "/v1/admin/catalog/mirror/refresh-prices", headers=admin_headers
        )
    )
    assert body["refreshed"] == {}
    assert no_upstream_calls["refresh"] == []
    # The status block only ever counts the Pokémon mirror, too.
    assert body["status"]["sets"] == 0


@pytest.mark.asyncio
async def test_an_unknown_admin_catalog_path_is_a_404(client, admin_headers):
    """Guard against a future prefix change silently swallowing the mirror
    routes into a catch-all."""
    resp = await client.get(f"/v1/admin/catalog/{uuid.uuid4()}", headers=admin_headers)
    assert resp.status_code == 404
