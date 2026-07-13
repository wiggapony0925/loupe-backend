"""Router tests for /v1/admin/carousels — the operator carousel controls.

Verifies admin gating and the full control surface: overview shape, live
toggle/edit/add/delete/reset through the kv_cache override document, the AI
kill switch, and the synchronous regenerate pass (model mocked).
"""

from __future__ import annotations

import contextlib

import pytest

from app.config import get_settings
from app.schemas.carousel import CarouselRecipe


@contextlib.contextmanager
def _as_admin(email: str):
    """Temporarily add an email to the admin allowlist (tests start empty)."""
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = email  # type: ignore[misc]
    try:
        yield
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


@pytest.mark.asyncio
async def test_carousels_require_admin(client, auth_headers):
    assert (await client.get("/v1/admin/carousels")).status_code in (401, 403)
    # Authenticated but not an admin is still rejected.
    resp = await client.get("/v1/admin/carousels", headers=auth_headers)
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_overview_shape(client, created_user, auth_headers):
    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/carousels", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert set(body) >= {"aiConfigured", "aiEnabled", "recipes", "games", "ai"}
    assert body["aiEnabled"] is True
    ids = [r["id"] for r in body["recipes"]]
    assert "grails" in ids and "steals5" in ids
    sample = body["recipes"][0]
    assert sample["origin"] == "file"
    assert {"enabled", "edited", "removed", "games"} <= set(sample)
    games = {g["id"]: g for g in body["games"]}
    assert games["pokemon"]["curatedCount"] > 0
    assert games["pokemon"]["activeSource"] == "curated"
    assert games["digimon"]["catalogOnly"] is True
    assert games["digimon"]["curatedCount"] == 0


@pytest.mark.asyncio
async def test_toggle_edit_and_reset(client, created_user, auth_headers):
    with _as_admin(created_user.email):
        resp = await client.put(
            "/v1/admin/carousels/grails",
            headers=auth_headers,
            json={"enabled": False, "title": "Ultra grails"},
        )
        assert resp.status_code == 200
        row = resp.json()["data"]
        assert row["enabled"] is False
        assert row["title"] == "Ultra grails"
        assert row["edited"] is True

        # The public serve path reflects the edit immediately.
        pub = await client.get("/v1/public/carousels", params={"game": "pokemon"})
        assert pub.status_code == 200
        served = [c["id"] for c in pub.json()["data"]["carousels"]]
        assert "grails" not in served

        resp = await client.post(
            "/v1/admin/carousels/grails/reset", headers=auth_headers
        )
        assert resp.status_code == 200
        row = resp.json()["data"]
        assert row["enabled"] is True and row["edited"] is False
        assert row["title"] == "Grails & chase cards"

        assert (
            await client.put(
                "/v1/admin/carousels/nope", headers=auth_headers, json={"enabled": True}
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_add_duplicate_and_delete_custom(client, created_user, auth_headers):
    recipe = {
        "id": "op-shelf",
        "title": "Operator shelf",
        "subtitle": "Hand-picked by the operator.",
        "source": "value",
        "priceMin": 10,
        "games": ["pokemon"],
    }
    with _as_admin(created_user.email):
        resp = await client.post(
            "/v1/admin/carousels", headers=auth_headers, json=recipe
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["origin"] == "custom"

        dup = await client.post(
            "/v1/admin/carousels", headers=auth_headers, json=recipe
        )
        assert dup.status_code == 409

        # Serves for its scoped game only.
        pub = await client.get("/v1/public/carousels", params={"game": "pokemon"})
        assert "op-shelf" in [c["id"] for c in pub.json()["data"]["carousels"]]
        pub = await client.get("/v1/public/carousels", params={"game": "magic"})
        assert "op-shelf" not in [c["id"] for c in pub.json()["data"]["carousels"]]

        gone = await client.delete("/v1/admin/carousels/op-shelf", headers=auth_headers)
        assert gone.status_code == 204
        overview = await client.get("/v1/admin/carousels", headers=auth_headers)
        assert "op-shelf" not in [r["id"] for r in overview.json()["data"]["recipes"]]


@pytest.mark.asyncio
async def test_delete_file_recipe_tombstones(client, created_user, auth_headers):
    with _as_admin(created_user.email):
        assert (
            await client.delete("/v1/admin/carousels/rainbow", headers=auth_headers)
        ).status_code == 204
        overview = (
            await client.get("/v1/admin/carousels", headers=auth_headers)
        ).json()["data"]
        row = next(r for r in overview["recipes"] if r["id"] == "rainbow")
        assert row["removed"] is True  # still listed, restorable
        pub = await client.get("/v1/public/carousels", params={"game": "pokemon"})
        assert "rainbow" not in [c["id"] for c in pub.json()["data"]["carousels"]]

        assert (
            await client.delete("/v1/admin/carousels/nope", headers=auth_headers)
        ).status_code == 404


@pytest.mark.asyncio
async def test_ai_kill_switch(client, created_user, auth_headers):
    with _as_admin(created_user.email):
        resp = await client.put(
            "/v1/admin/carousels/ai", headers=auth_headers, json={"enabled": False}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["aiEnabled"] is False
        resp = await client.put(
            "/v1/admin/carousels/ai", headers=auth_headers, json={"enabled": True}
        )
        assert resp.json()["data"]["aiEnabled"] is True


@pytest.mark.asyncio
async def test_regenerate(client, created_user, auth_headers, monkeypatch):
    from app.services.catalog import carousel_service

    with _as_admin(created_user.email):
        # Unknown game → 404; no model configured → 400.
        assert (
            await client.post(
                "/v1/admin/carousels/regenerate",
                headers=auth_headers,
                params={"game": "sports"},
            )
        ).status_code == 404
        monkeypatch.setattr(carousel_service, "configured", lambda: False)
        assert (
            await client.post(
                "/v1/admin/carousels/regenerate",
                headers=auth_headers,
                params={"game": "pokemon"},
            )
        ).status_code == 400

        # Configured + model answers → the design caches and the overview
        # reports the game as AI-active.
        monkeypatch.setattr(carousel_service, "configured", lambda: True)

        async def fake_generate(game: str, label: str) -> list[CarouselRecipe]:
            return [CarouselRecipe(id="ai-shelf", title="AI shelf", subtitle="s")]

        monkeypatch.setattr(carousel_service, "_generate_ai", fake_generate)
        resp = await client.post(
            "/v1/admin/carousels/regenerate",
            headers=auth_headers,
            params={"game": "pokemon"},
        )
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["source"] == "ai"
        assert [c["id"] for c in body["carousels"]] == ["ai-shelf"]

        overview = (
            await client.get("/v1/admin/carousels", headers=auth_headers)
        ).json()["data"]
        assert overview["ai"]["pokemon"][0]["id"] == "ai-shelf"
        games = {g["id"]: g for g in overview["games"]}
        assert games["pokemon"]["activeSource"] == "ai"
        assert games["pokemon"]["aiCount"] == 1

        # An empty model answer surfaces as a 502, not a silent overwrite.
        async def empty_generate(game: str, label: str) -> list[CarouselRecipe]:
            return []

        monkeypatch.setattr(carousel_service, "_generate_ai", empty_generate)
        assert (
            await client.post(
                "/v1/admin/carousels/regenerate",
                headers=auth_headers,
                params={"game": "magic"},
            )
        ).status_code == 502
