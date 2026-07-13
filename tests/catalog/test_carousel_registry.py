"""Tests for the operator-controlled carousel registry.

Covers the three layers: the checked-in JSON file (load + validation), the
kv_cache override document (persistence + merge semantics), and the serve-time
filtering (``recipes_for``) that both public endpoints consume.
"""

from __future__ import annotations

import pytest

from app.schemas.carousel import CarouselOverrides, RegistryRecipe
from app.services.catalog import carousel_registry, carousel_service
from app.services.catalog.carousel_registry import (
    DuplicateRecipeError,
    UnknownRecipeError,
)

STOCK = CarouselOverrides()


# ── File registry ──


def test_file_registry_loads_and_validates() -> None:
    ids = [r.id for r in carousel_registry.FILE_RECIPES]
    assert len(ids) == len(set(ids)), "file registry ids must be unique"
    assert {"grails", "steals5", "rainbow", "blue-chips"} <= set(ids)
    # Every entry is a full RegistryRecipe: enabled by default, priced-games scope.
    assert all(r.enabled for r in carousel_registry.FILE_RECIPES)
    assert all(r.games is None for r in carousel_registry.FILE_RECIPES)
    # The JSON round-trips regex escapes intact (\\b in the source file).
    holo = next(r for r in carousel_registry.FILE_RECIPES if r.id == "holo-hits")
    assert "\\b" in (holo.rarityPattern or "")


# ── Merge semantics (pure — no DB needed) ──


def test_merged_registry_applies_edits_and_flags() -> None:
    o = CarouselOverrides(edits={"grails": {"title": "Ultra grails", "enabled": False}})
    merged = {r.id: r for r in carousel_registry.merged_registry(o)}
    assert merged["grails"].title == "Ultra grails"
    assert merged["grails"].enabled is False
    assert merged["grails"].edited is True
    assert merged["rainbow"].edited is False


def test_merged_registry_tombstones_and_customs() -> None:
    custom = RegistryRecipe(
        id="operator-pick", title="Operator pick", subtitle="s", games=["pokemon"]
    )
    o = CarouselOverrides(removed=["rainbow"], added=[custom])
    merged = carousel_registry.merged_registry(o)
    by_id = {r.id: r for r in merged}
    # File recipe stays listed (restorable) but flagged removed.
    assert by_id["rainbow"].removed is True
    assert by_id["rainbow"].origin == "file"
    # Operator recipe is appended with the custom origin.
    assert merged[-1].id == "operator-pick"
    assert merged[-1].origin == "custom"


def test_merged_registry_ignores_stale_invalid_edit() -> None:
    # A patch that no longer validates (schema drift) must not break the shelf —
    # the file version serves instead.
    o = CarouselOverrides(edits={"grails": {"priceMin": -5}})
    merged = {r.id: r for r in carousel_registry.merged_registry(o)}
    assert merged["grails"].priceMin == 250
    assert merged["grails"].edited is True  # still surfaced as edited in the portal


def test_recipes_for_filters_enabled_removed_and_scope() -> None:
    custom = RegistryRecipe(
        id="op-digimon",
        title="Digimon picks",
        subtitle="s",
        source="catalog",
        games=["digimon"],
    )
    o = CarouselOverrides(
        edits={"grails": {"enabled": False}}, removed=["rainbow"], added=[custom]
    )
    pokemon = [r.id for r in carousel_registry.recipes_for("pokemon", o)]
    assert "grails" not in pokemon  # disabled
    assert "rainbow" not in pokemon  # deleted
    assert "op-digimon" not in pokemon  # scoped elsewhere
    assert "premium" in pokemon
    # An explicit games list can target a catalog-only game.
    assert [r.id for r in carousel_registry.recipes_for("digimon", o)] == ["op-digimon"]
    # The wire shape is the plain CarouselRecipe (no operator fields leak).
    assert "enabled" not in carousel_registry.recipes_for("pokemon", o)[0].model_dump()


# ── Override document persistence + operator ops (kv_cache-backed) ──


@pytest.mark.asyncio
async def test_overrides_default_when_unset(db_engine) -> None:
    o = await carousel_registry.get_overrides()
    assert o == CarouselOverrides()
    assert o.aiEnabled is True


@pytest.mark.asyncio
async def test_update_recipe_stores_diff_only_patch(db_engine) -> None:
    view = await carousel_registry.update_recipe("grails", {"enabled": False})
    assert view.enabled is False and view.edited is True
    # Re-enabling restores the file value exactly → the patch prunes to nothing.
    view = await carousel_registry.update_recipe("grails", {"enabled": True})
    assert view.enabled is True and view.edited is False
    assert (await carousel_registry.get_overrides()).edits == {}


@pytest.mark.asyncio
async def test_update_unknown_recipe_raises(db_engine) -> None:
    with pytest.raises(UnknownRecipeError):
        await carousel_registry.update_recipe("nope", {"enabled": False})


@pytest.mark.asyncio
async def test_add_update_delete_custom_recipe(db_engine) -> None:
    recipe = RegistryRecipe(
        id="op-shelf", title="Operator shelf", subtitle="s", priceMin=10
    )
    view = await carousel_registry.add_recipe(recipe)
    assert view.origin == "custom"
    with pytest.raises(DuplicateRecipeError):
        await carousel_registry.add_recipe(recipe)
    with pytest.raises(DuplicateRecipeError):
        await carousel_registry.add_recipe(
            RegistryRecipe(id="grails", title="X", subtitle="s")
        )
    view = await carousel_registry.update_recipe("op-shelf", {"title": "Renamed"})
    assert view.title == "Renamed" and view.origin == "custom"
    await carousel_registry.delete_recipe("op-shelf")
    o = await carousel_registry.get_overrides()
    assert o.added == []
    assert "op-shelf" not in [r.id for r in carousel_registry.merged_registry(o)]


@pytest.mark.asyncio
async def test_delete_and_reset_file_recipe(db_engine) -> None:
    await carousel_registry.update_recipe("rainbow", {"title": "Edited"})
    await carousel_registry.delete_recipe("rainbow")
    o = await carousel_registry.get_overrides()
    assert "rainbow" in o.removed
    assert "rainbow" not in [r.id for r in carousel_registry.recipes_for("pokemon", o)]
    # Reset clears both the tombstone and the edit.
    view = await carousel_registry.reset_recipe("rainbow")
    assert view.removed is False and view.edited is False
    assert view.title == "Rainbow & secret rares"
    with pytest.raises(UnknownRecipeError):
        await carousel_registry.reset_recipe("nope")


@pytest.mark.asyncio
async def test_corrupt_override_document_falls_back(db_engine) -> None:
    from app.platform.cache_l2 import kv_set

    await kv_set(carousel_registry.OVERRIDES_KEY, '{"edits": "not-a-dict"}', 60)
    assert await carousel_registry.get_overrides() == CarouselOverrides()


# ── Serve path honors the overrides live ──


@pytest.mark.asyncio
async def test_get_carousels_serves_operator_edits(db_engine, monkeypatch) -> None:
    monkeypatch.setattr(carousel_service, "configured", lambda: False)
    await carousel_registry.update_recipe("grails", {"enabled": False})
    resp = await carousel_service.get_carousels("pokemon")
    assert resp.source == "curated"
    assert "grails" not in [c.id for c in resp.carousels]


@pytest.mark.asyncio
async def test_ai_kill_switch_pins_curated(db_engine, monkeypatch) -> None:
    from app.schemas.carousel import CarouselRecipe, CarouselResponse

    # Even with an AI design cached and a model configured, the kill switch
    # serves the curated registry and never spawns generation.
    await carousel_service._cache_set(
        CarouselResponse(
            game="pokemon",
            source="ai",
            carousels=[CarouselRecipe(id="ai-shelf", title="AI", subtitle="s")],
        ),
        3600,
    )
    monkeypatch.setattr(carousel_service, "configured", lambda: True)
    spawned: list[str] = []
    monkeypatch.setattr(
        carousel_service, "_spawn_generation", lambda g, label: spawned.append(g)
    )
    await carousel_registry.set_ai_enabled(False)
    resp = await carousel_service.get_carousels("pokemon")
    assert resp.source == "curated"
    assert spawned == []
    # Flip it back on → the cached AI design serves again.
    await carousel_registry.set_ai_enabled(True)
    resp = await carousel_service.get_carousels("pokemon")
    assert resp.source == "ai"
