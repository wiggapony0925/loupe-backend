"""Edge scenarios for the social layer: claim-gates, discovery, badges,
case-insensitive handles, and payload deep-link fields."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.social.models import SocialFollow, SocialFollowRequest
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.social.test_social_api import _add_graded_card, _claim, _headers


@pytest.mark.asyncio
async def test_follow_requires_claimed_profile(client, created_user, second_user):
    """A viewer with no handle can't enter the graph (counts vs lists drift)."""
    await _claim(client, second_user, "target")
    resp = await client.post(
        "/v1/social/users/target/follow", headers=_headers(created_user)
    )
    assert_envelope_error(resp, expected_status=409)

    # After claiming, the same follow goes through.
    await _claim(client, created_user, "nowclaimed")
    resp = await client.post(
        "/v1/social/users/target/follow", headers=_headers(created_user)
    )
    assert assert_envelope_ok(resp)["relationship"] == "following"


@pytest.mark.asyncio
async def test_profile_lookup_is_case_insensitive(client, created_user, second_user):
    await _claim(client, created_user, "CamelCase")
    resp = await client.get("/v1/social/users/CAMELCASE", headers=_headers(second_user))
    assert assert_envelope_ok(resp)["username"] == "camelcase"


@pytest.mark.asyncio
async def test_suggested_excludes_followed_and_self(
    client, created_user, second_user, db_session
):
    await _claim(client, created_user, "me_myself")
    await _claim(client, second_user, "fresh_face")

    resp = await client.get("/v1/social/suggested", headers=_headers(created_user))
    names = [r["username"] for r in assert_envelope_ok(resp)]
    assert "fresh_face" in names
    assert "me_myself" not in names  # never suggest yourself

    # Following them removes them from suggestions.
    await client.post(
        "/v1/social/users/fresh_face/follow", headers=_headers(created_user)
    )
    resp = await client.get("/v1/social/suggested", headers=_headers(created_user))
    assert "fresh_face" not in [r["username"] for r in assert_envelope_ok(resp)]


@pytest.mark.asyncio
async def test_suggested_includes_private_requested_excluded(
    client, created_user, second_user
):
    await _claim(client, created_user, "asker2")
    await _claim(client, second_user, "shyguy", is_private=True)

    # Private profiles are suggestable (following becomes a request)…
    resp = await client.get("/v1/social/suggested", headers=_headers(created_user))
    assert "shyguy" in [r["username"] for r in assert_envelope_ok(resp)]

    # …until a request is pending.
    await client.post("/v1/social/users/shyguy/follow", headers=_headers(created_user))
    resp = await client.get("/v1/social/suggested", headers=_headers(created_user))
    assert "shyguy" not in [r["username"] for r in assert_envelope_ok(resp)]


@pytest.mark.asyncio
async def test_pro_badge_reflects_raw_plan(
    client, created_user, second_user, db_session
):
    await _claim(client, created_user, "probadge")
    await _claim(client, second_user, "viewer2")
    created_user.plan = "pro"
    await db_session.commit()

    resp = await client.get("/v1/social/users/probadge", headers=_headers(second_user))
    assert assert_envelope_ok(resp)["is_pro"] is True

    resp = await client.get(
        "/v1/social/search", params={"q": "probadge"}, headers=_headers(second_user)
    )
    assert assert_envelope_ok(resp)[0]["is_pro"] is True


@pytest.mark.asyncio
async def test_collection_items_carry_card_id(
    client, created_user, second_user, db_session
):
    await _claim(client, created_user, "linker")
    await _claim(client, second_user, "viewer3")
    row = await _add_graded_card(db_session, created_user)

    resp = await client.get(
        "/v1/social/users/linker/collection", headers=_headers(second_user)
    )
    items = assert_envelope_ok(resp)["items"]
    assert items[0]["card_id"] == str(row.card_id)


@pytest.mark.asyncio
async def test_unfollow_when_not_following_is_noop(client, created_user, second_user):
    await _claim(client, created_user, "calm")
    await _claim(client, second_user, "stranger2")
    resp = await client.delete(
        "/v1/social/users/stranger2/follow", headers=_headers(created_user)
    )
    assert assert_envelope_ok(resp)["relationship"] == "none"


@pytest.mark.asyncio
async def test_deactivate_severs_everything_and_frees_the_handle(
    client, created_user, second_user
):
    await _claim(client, created_user, "leaver")
    await _claim(client, second_user, "stayer")

    # Build edges in both directions.
    await client.post("/v1/social/users/stayer/follow", headers=_headers(created_user))
    await client.post("/v1/social/users/leaver/follow", headers=_headers(second_user))

    resp = await client.delete("/v1/social/me", headers=_headers(created_user))
    assert resp.status_code == 204

    # Gone from search; the other side's counts no longer include them.
    resp = await client.get(
        "/v1/social/search", params={"q": "leaver"}, headers=_headers(second_user)
    )
    assert assert_envelope_ok(resp) == []
    resp = await client.get("/v1/social/users/stayer", headers=_headers(second_user))
    view = assert_envelope_ok(resp)
    assert view["follower_count"] == 0
    assert view["following_count"] == 0

    # The handle is claimable again — by anyone.
    resp = await client.put(
        "/v1/social/me", json={"username": "leaver"}, headers=_headers(second_user)
    )
    assert assert_envelope_ok(resp)["username"] == "leaver"

    # Deactivating twice is a clean 404, not a 500.
    resp = await client.delete("/v1/social/me", headers=_headers(created_user))
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_remove_follower_kicks_but_does_not_block(
    client, created_user, second_user
):
    await _claim(client, created_user, "kicker")
    await _claim(client, second_user, "clingy")
    await client.post("/v1/social/users/kicker/follow", headers=_headers(second_user))

    resp = await client.delete(
        "/v1/social/me/followers/clingy", headers=_headers(created_user)
    )
    assert resp.status_code == 204

    # The edge is gone from both sides…
    resp = await client.get("/v1/social/users/kicker", headers=_headers(second_user))
    view = assert_envelope_ok(resp)
    assert view["follower_count"] == 0
    assert view["relationship"] == "none"

    # …but they aren't blocked: following again works instantly.
    resp = await client.post(
        "/v1/social/users/kicker/follow", headers=_headers(second_user)
    )
    assert assert_envelope_ok(resp)["relationship"] == "following"

    # Removing someone who doesn't follow you is a clean 404.
    resp = await client.delete(
        "/v1/social/me/followers/kicker", headers=_headers(created_user)
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_friend_owners_shows_only_people_i_follow(
    client, db_session, created_user, second_user
):
    from tests.factories import make_user

    await _claim(client, created_user, "browser1")
    await _claim(client, second_user, "friendly")
    stranger = await make_user(db_session)
    stranger_headers = _headers(stranger)
    await client.put(
        "/v1/social/me", json={"username": "stranger9"}, headers=stranger_headers
    )

    # friendly (followed) owns TWO copies; stranger9 (not followed) owns one.
    row = await _add_graded_card(db_session, second_user)
    from decimal import Decimal as _D

    from app.models.grade import GradedCard as _GC

    db_session.add(_GC(user_id=second_user.id, card_id=row.card_id, grade=_D("8.0")))
    db_session.add(_GC(user_id=stranger.id, card_id=row.card_id, grade=_D("7.0")))
    await db_session.commit()

    await client.post(
        "/v1/social/users/friendly/follow", headers=_headers(created_user)
    )

    resp = await client.get(
        f"/v1/social/cards/{row.card_id}/owners", headers=_headers(created_user)
    )
    owners = assert_envelope_ok(resp)
    assert [o["username"] for o in owners] == ["friendly"]
    assert owners[0]["copies"] == 2
    assert owners[0]["relationship"] == "following"

    # Unknown ref → empty list, not an error.
    resp = await client.get(
        "/v1/social/cards/nosuch:ref-123/owners", headers=_headers(created_user)
    )
    assert assert_envelope_ok(resp) == []


@pytest.mark.asyncio
async def test_collection_includes_set_breakdown(
    client, db_session, created_user, second_user
):
    """Sets ride the collection payload — "they have 5 Evolving Skies"."""
    from decimal import Decimal as _D

    from app.models.card import Card, CardSet
    from app.models.enums import TcgEnum
    from app.models.grade import GradedCard as _GC

    await _claim(client, created_user, "setowner")
    await _claim(client, second_user, "setviewer")

    async def add(set_row, name, value):
        card = Card(
            set_id=set_row.id if set_row else None,
            tcg=TcgEnum.pokemon,
            name=name,
            image_url=f"https://img.example/{name}.png",
        )
        db_session.add(card)
        await db_session.flush()
        db_session.add(
            _GC(
                user_id=created_user.id,
                card_id=card.id,
                grade=_D("9.0"),
                estimated_value_usd=_D(value),
            )
        )

    skies = CardSet(tcg=TcgEnum.pokemon, name="Evolving Skies", code="EVS")
    base = CardSet(tcg=TcgEnum.pokemon, name="Base Set", code="BS")
    promos = CardSet(tcg=TcgEnum.pokemon, name="Promos", code="PR")
    db_session.add_all([skies, base, promos])
    await db_session.flush()
    await add(skies, "Umbreon", "600")
    await add(skies, "Sylveon", "90")
    await add(base, "Charizard", "400")
    await add(promos, "Mystery", "10")
    await db_session.commit()

    resp = await client.get(
        "/v1/social/users/setowner/collection", headers=_headers(second_user)
    )
    data = assert_envelope_ok(resp)
    sets = data["sets"]
    # Ordered by set value: Skies (690) > Base (400) > Promos (10).
    assert [s["name"] for s in sets] == ["Evolving Skies", "Base Set", "Promos"]
    assert data["total_sets"] == 3  # server-side cap metadata
    assert [s["count"] for s in sets] == [2, 1, 1]
    # Cover = the set's most valuable card's art.
    assert sets[0]["cover_image_url"].endswith("Umbreon.png")
    assert float(sets[0]["estimated_value_usd"]) == 690.0


@pytest.mark.asyncio
async def test_collection_includes_curated_portfolios(
    client, db_session, created_user, second_user
):
    """Profiles surface the user's CURATED collections (binders), which is
    what collectors mean by "my collections" — not catalog sets."""
    from decimal import Decimal as _D

    from app.models.collection import Collection, CollectionItem
    from app.models.grade import GradedCard as _GC
    from tests.factories import make_card

    await _claim(client, created_user, "binderowner")
    await _claim(client, second_user, "binderviewer")

    binder = Collection(user_id=created_user.id, name="PC Binder", color="#7cf")
    empty = Collection(user_id=created_user.id, name="For Trade")
    db_session.add_all([binder, empty])
    await db_session.flush()

    card = await make_card(db_session, name="Cover Star")
    card.image_url = "https://img.example/cover-star.png"
    row = _GC(
        user_id=created_user.id,
        card_id=card.id,
        grade=_D("9.5"),
        estimated_value_usd=_D("250"),
    )
    db_session.add(row)
    await db_session.flush()
    db_session.add(CollectionItem(collection_id=binder.id, graded_card_id=row.id))
    await db_session.commit()

    resp = await client.get(
        "/v1/social/users/binderowner/collection", headers=_headers(second_user)
    )
    data = assert_envelope_ok(resp)
    ports = data["portfolios"]
    assert [p["name"] for p in ports] == ["PC Binder", "For Trade"]
    assert ports[0]["count"] == 1
    assert float(ports[0]["estimated_value_usd"]) == 250.0
    assert ports[0]["cover_image_url"].endswith("cover-star.png")
    assert ports[1]["count"] == 0


@pytest.mark.asyncio
async def test_discover_is_ranked_and_disjoint(
    client, db_session, created_user, second_user
):
    """/discover composes the Community page server-side: most-followed
    first, featured/more disjoint, followed collectors excluded."""
    from tests.factories import make_user

    await _claim(client, created_user, "watcher")
    await _claim(client, second_user, "popular")
    third = await make_user(db_session)
    await client.put(
        "/v1/social/me", json={"username": "quiet"}, headers=_headers(third)
    )
    # Make "popular" popular: quiet follows them.
    await client.post("/v1/social/users/popular/follow", headers=_headers(third))

    resp = await client.get("/v1/social/discover", headers=_headers(created_user))
    data = assert_envelope_ok(resp)
    names = [u["username"] for u in data["featured"]] + [
        u["username"] for u in data["more"]
    ]
    assert names[0] == "popular"  # ranked by follower count first
    assert "watcher" not in names  # never yourself
    assert len(names) == len(set(names))  # disjoint + unique

    # Following someone removes them from the next compose.
    await client.post("/v1/social/users/popular/follow", headers=_headers(created_user))
    resp = await client.get("/v1/social/discover", headers=_headers(created_user))
    data = assert_envelope_ok(resp)
    names = [u["username"] for u in data["featured"]] + [
        u["username"] for u in data["more"]
    ]
    assert "popular" not in names


@pytest.mark.asyncio
async def test_at_me_alias_resolves_server_side(client, created_user, second_user):
    """`/users/@me` (and `me`) resolve to the caller's own profile — clients
    never need to learn their own handle before linking to it."""
    resp = await client.get("/v1/social/users/@me", headers=_headers(created_user))
    assert_envelope_error(resp, expected_status=404)  # unclaimed yet

    await _claim(client, created_user, "aliasme")
    resp = await client.get("/v1/social/users/@me", headers=_headers(created_user))
    view = assert_envelope_ok(resp)
    assert view["username"] == "aliasme"
    assert view["relationship"] == "self"

    # The alias is ALWAYS the caller — never someone else's profile.
    await _claim(client, second_user, "notme")
    resp = await client.get("/v1/social/users/me", headers=_headers(second_user))
    assert assert_envelope_ok(resp)["username"] == "notme"


@pytest.mark.asyncio
async def test_collection_includes_sealed_category(
    client, db_session, created_user, second_user
):
    """Sealed products are a first-class category on shared profiles, valued
    exactly like /v1/grades/summary: unopened only, unit value x quantity —
    and the combined headline (cards + sealed) rides the payload."""
    from datetime import UTC, datetime
    from decimal import Decimal as _D

    from app.models.enums import SealedProductTypeEnum, TcgEnum
    from app.models.sealed import SealedHolding, SealedProduct

    await _claim(client, created_user, "sealedowner")
    await _claim(client, second_user, "sealedviewer")
    await _add_graded_card(db_session, created_user, "100.00")

    product = SealedProduct(
        tcg=TcgEnum.pokemon,
        product_type=SealedProductTypeEnum.booster_box,
        name="151 Booster Box",
        set_name="Scarlet & Violet 151",
        image_url="https://img.example/151-bb.png",
    )
    db_session.add(product)
    await db_session.flush()
    db_session.add_all(
        [
            SealedHolding(  # 2 x $200 → $400 in the rollup
                user_id=created_user.id,
                product_id=product.id,
                quantity=2,
                purchase_price_usd=_D("150.00"),
                estimated_value_usd=_D("200.00"),
            ),
            SealedHolding(  # opened → excluded
                user_id=created_user.id,
                product_id=product.id,
                quantity=1,
                estimated_value_usd=_D("500.00"),
                opened_at=datetime.now(UTC),
            ),
            SealedHolding(  # deleted → excluded
                user_id=created_user.id,
                product_id=product.id,
                quantity=1,
                estimated_value_usd=_D("900.00"),
                deleted_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(
        "/v1/social/users/sealedowner/collection", headers=_headers(second_user)
    )
    data = assert_envelope_ok(resp)
    assert data["sealed_count"] == 2
    assert float(data["sealed_value_usd"]) == 400.0
    # Headline = cards ($100) + sealed ($400).
    assert float(data["total_value_usd"]) == 500.0
    assert len(data["sealed"]) == 1
    tile = data["sealed"][0]
    assert tile["name"] == "151 Booster Box"
    assert tile["product_type"] == "booster_box"
    assert tile["quantity"] == 2
    assert float(tile["estimated_value_usd"]) == 400.0
    # Cost basis stays private, exactly like card items.
    assert "purchase_price_usd" not in tile


@pytest.mark.asyncio
async def test_portfolio_drilldown_endpoint(
    client, db_session, created_user, second_user
):
    """GET /users/{u}/collections/{id} — one binder's cards, value-first;
    404 on foreign/unknown binder ids, 403 behind the privacy gate."""
    import uuid as _uuid

    from app.models.collection import Collection, CollectionItem

    await _claim(client, created_user, "drillowner")
    await _claim(client, second_user, "drillviewer")

    binder = Collection(user_id=created_user.id, name="Eeveelutions", color="#7cf")
    other = Collection(user_id=created_user.id, name="Everything Else")
    db_session.add_all([binder, other])
    await db_session.flush()

    cheap = await _add_graded_card(db_session, created_user, "50.00")
    pricey = await _add_graded_card(db_session, created_user, "300.00")
    stray = await _add_graded_card(db_session, created_user, "999.00")
    db_session.add_all(
        [
            CollectionItem(collection_id=binder.id, graded_card_id=cheap.id),
            CollectionItem(collection_id=binder.id, graded_card_id=pricey.id),
            CollectionItem(collection_id=other.id, graded_card_id=stray.id),
        ]
    )
    await db_session.commit()

    resp = await client.get(
        f"/v1/social/users/drillowner/collections/{binder.id}",
        headers=_headers(second_user),
    )
    data = assert_envelope_ok(resp)
    assert data["name"] == "Eeveelutions"
    assert data["count"] == 2
    assert float(data["estimated_value_usd"]) == 350.0
    values = [float(i["estimated_value_usd"]) for i in data["items"]]
    assert values == [300.0, 50.0]  # value-first, the stray card absent

    missing = await client.get(
        f"/v1/social/users/drillowner/collections/{_uuid.uuid4()}",
        headers=_headers(second_user),
    )
    assert_envelope_error(missing, expected_status=404)

    # Private + not following → the binder is invisible too.
    await client.put(
        "/v1/social/me",
        json={"username": "drillowner", "is_private": True},
        headers=_headers(created_user),
    )
    gated = await client.get(
        f"/v1/social/users/drillowner/collections/{binder.id}",
        headers=_headers(second_user),
    )
    assert_envelope_error(gated, expected_status=403)


# ── RULE: a pending follow request to a PUBLIC account is not a valid state ──
#
# Enforced as an invariant rather than a one-time fix-up. The private→public
# transition already converted requests, but anything reaching "public" by
# another route (an older build, a restore, a seed, a manual edit) left
# requests stuck forever — visible in the inbox and impossible to clear.
# These lock down both the rule and every edge case it has to survive.


@pytest.mark.asyncio
async def test_going_public_auto_accepts_everyone_who_asked(
    client, created_user, second_user
):
    await _claim(client, created_user, "gatekeeper", is_private=True)
    await _claim(client, second_user, "asker")

    assert_envelope_ok(
        await client.post(
            "/v1/social/users/gatekeeper/follow", headers=_headers(second_user)
        )
    )
    # Flip to public.
    await _claim(client, created_user, "gatekeeper", is_private=False)

    me = assert_envelope_ok(
        await client.get("/v1/social/me", headers=_headers(created_user))
    )
    assert me["incoming_request_count"] == 0

    view = assert_envelope_ok(
        await client.get("/v1/social/users/gatekeeper", headers=_headers(second_user))
    )
    assert view["relationship"] == "following", "the ask became a follow"
    assert view["follower_count"] == 1


@pytest.mark.asyncio
async def test_a_public_account_heals_requests_it_should_never_have_had(
    client, db_session, created_user, second_user
):
    """The reported bug: a PUBLIC account showing pending requests.

    Simulates state that reached "public" without passing through the
    transition — exactly what an older build or a restore leaves behind.
    """
    await _claim(client, created_user, "public_guy")
    await _claim(client, second_user, "asker")
    db_session.add(
        SocialFollowRequest(requester_id=second_user.id, target_id=created_user.id)
    )
    await db_session.commit()

    # Merely READING my own profile must resolve it — that endpoint draws
    # the inbox badge, so it cannot report a number that can't be acted on.
    me = assert_envelope_ok(
        await client.get("/v1/social/me", headers=_headers(created_user))
    )
    assert me["incoming_request_count"] == 0

    inbox = assert_envelope_ok(
        await client.get("/v1/social/requests", headers=_headers(created_user))
    )
    assert inbox == []

    view = assert_envelope_ok(
        await client.get("/v1/social/users/public_guy", headers=_headers(second_user))
    )
    assert view["relationship"] == "following"


@pytest.mark.asyncio
async def test_going_public_when_a_requester_already_follows(
    client, db_session, created_user, second_user
):
    """The duplicate that used to 500 the whole privacy change.

    The old inline conversion added a follow edge per request with no
    duplicate check; a requester who already followed violated the follow
    table's composite PK and took the entire request down with it.
    """
    await _claim(client, created_user, "gatekeeper", is_private=True)
    await _claim(client, second_user, "asker")
    db_session.add(
        SocialFollow(follower_id=second_user.id, followee_id=created_user.id)
    )
    db_session.add(
        SocialFollowRequest(requester_id=second_user.id, target_id=created_user.id)
    )
    await db_session.commit()

    # Must not raise, and must leave exactly ONE follow edge.
    await _claim(client, created_user, "gatekeeper", is_private=False)

    view = assert_envelope_ok(
        await client.get("/v1/social/users/gatekeeper", headers=_headers(second_user))
    )
    assert view["relationship"] == "following"
    assert view["follower_count"] == 1, "no duplicate edge"


@pytest.mark.asyncio
async def test_a_deleted_requester_is_dropped_not_followed(
    client, db_session, created_user, second_user
):
    """A dead account must not silently become a follower."""
    await _claim(client, created_user, "gatekeeper", is_private=True)
    await _claim(client, second_user, "ghost")
    db_session.add(
        SocialFollowRequest(requester_id=second_user.id, target_id=created_user.id)
    )
    second_user.deleted_at = datetime.now(UTC)
    await db_session.commit()

    await _claim(client, created_user, "gatekeeper", is_private=False)

    me = assert_envelope_ok(
        await client.get("/v1/social/me", headers=_headers(created_user))
    )
    assert me["incoming_request_count"] == 0, "the request is gone"
    followers = await db_session.execute(
        select(func.count())
        .select_from(SocialFollow)
        .where(SocialFollow.followee_id == created_user.id)
    )
    assert followers.scalar_one() == 0, "and produced no follow"


@pytest.mark.asyncio
async def test_a_requester_who_left_the_community_is_dropped(
    client, db_session, created_user, second_user
):
    """Deactivating removes the SocialProfile but the account lives on.

    The graph only holds collectors with claimed handles: a follow edge for
    a profile-less user would inflate the follower COUNT past what the
    follower LIST (a profiles join) can show.
    """
    await _claim(client, created_user, "gatekeeper", is_private=True)
    await _claim(client, second_user, "quitter")
    db_session.add(
        SocialFollowRequest(requester_id=second_user.id, target_id=created_user.id)
    )
    await db_session.commit()
    # They leave the community (profile gone, account intact).
    await client.delete("/v1/social/me", headers=_headers(second_user))

    await _claim(client, created_user, "gatekeeper", is_private=False)

    followers = await db_session.execute(
        select(func.count())
        .select_from(SocialFollow)
        .where(SocialFollow.followee_id == created_user.id)
    )
    assert followers.scalar_one() == 0


@pytest.mark.asyncio
async def test_healing_is_idempotent(client, db_session, created_user, second_user):
    """It runs on every read of my own profile, so it must be a no-op the
    second time — and must never multiply follower counts."""
    await _claim(client, created_user, "public_guy")
    await _claim(client, second_user, "asker")
    db_session.add(
        SocialFollowRequest(requester_id=second_user.id, target_id=created_user.id)
    )
    await db_session.commit()

    for _ in range(3):
        assert_envelope_ok(
            await client.get("/v1/social/me", headers=_headers(created_user))
        )

    view = assert_envelope_ok(
        await client.get("/v1/social/users/public_guy", headers=_headers(second_user))
    )
    assert view["follower_count"] == 1


@pytest.mark.asyncio
async def test_a_private_account_keeps_its_pending_requests(
    client, created_user, second_user
):
    """The guard rail: healing must never touch an account that is still
    private — that is exactly where pending requests belong."""
    await _claim(client, created_user, "gatekeeper", is_private=True)
    await _claim(client, second_user, "asker")
    assert_envelope_ok(
        await client.post(
            "/v1/social/users/gatekeeper/follow", headers=_headers(second_user)
        )
    )

    me = assert_envelope_ok(
        await client.get("/v1/social/me", headers=_headers(created_user))
    )
    assert me["incoming_request_count"] == 1

    inbox = assert_envelope_ok(
        await client.get("/v1/social/requests", headers=_headers(created_user))
    )
    assert len(inbox) == 1, "still the owner's decision to make"
