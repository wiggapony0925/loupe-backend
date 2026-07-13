"""Operator-controlled carousel registry.

The curated shelf pool lives in a checked-in JSON file
(``carousel_registry.json``) — one entry per recipe with an ``enabled`` flag
and ``games`` scoping — validated through :class:`RegistryRecipe` at import so
a malformed file fails CI, not a request.

Operators get *live* control over it from the dev portal WITHOUT a deploy or a
DB migration: their edits (enable/disable, field patches, adds, deletes, plus
the AI-shelf kill switch) are one :class:`CarouselOverrides` JSON document in
``kv_cache`` (Postgres — shared by every instance, survives restarts) under a
well-known key, merged over the file at serve time. Deleted *file* recipes are
tombstoned rather than erased so the portal can always restore them; deleted
*custom* recipes are simply dropped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.platform.cache_l2 import kv_get, kv_set
from app.schemas.carousel import (
    AdminRecipe,
    CarouselOverrides,
    CarouselRecipe,
    RegistryRecipe,
)
from app.utils.logger import get_logger

logger = get_logger("services.carousel_registry")

GAME_LABELS: dict[str, str] = {
    "pokemon": "Pokémon",
    "magic": "Magic: The Gathering",
    "yugioh": "Yu-Gi-Oh!",
    "onepiece": "One Piece",
    "digimon": "Digimon",
}

#: Games whose catalog has no price feed yet (mirrors
#: ``trending_service._CATALOG_ONLY_SOURCE``). Value/price shelves are
#: meaningless for them, so a recipe with ``games=None`` never serves there —
#: an operator can still target them explicitly (e.g. a ``catalog`` shelf).
CATALOG_ONLY_GAMES: frozenset[str] = frozenset({"onepiece", "digimon"})
#: Games that actually have a priced discovery pool.
PRICED_GAMES: frozenset[str] = frozenset(GAME_LABELS) - CATALOG_ONLY_GAMES

_REGISTRY_PATH = Path(__file__).with_name("carousel_registry.json")

#: Where the operator override document lives in ``kv_cache``.
OVERRIDES_KEY = "carousels:operator_overrides:v1"
#: ``kv_cache`` rows need an expiry; overrides are settings, not cache — use an
#: effectively-forever TTL, refreshed on every save.
_OVERRIDES_TTL = 10 * 365 * 24 * 60 * 60


def _load_file_registry() -> list[RegistryRecipe]:
    """Parse + validate the checked-in registry. Raises at import on a bad
    file so a broken deploy is caught by CI/startup, never by a shopper."""
    doc = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    recipes = [RegistryRecipe.model_validate(item) for item in doc["recipes"]]
    ids = [r.id for r in recipes]
    if len(set(ids)) != len(ids):
        raise ValueError(f"carousel_registry.json has duplicate recipe ids: {ids}")
    return recipes


#: The checked-in pool, loaded once at import.
FILE_RECIPES: list[RegistryRecipe] = _load_file_registry()
_FILE_BY_ID: dict[str, RegistryRecipe] = {r.id: r for r in FILE_RECIPES}


# ──────────────────────────────────────────────────────────────────────────
# Override document
# ──────────────────────────────────────────────────────────────────────────


async def get_overrides() -> CarouselOverrides:
    """The live operator override document (defaults when unset/corrupt)."""
    raw = await kv_get(OVERRIDES_KEY)
    if raw:
        try:
            return CarouselOverrides.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("carousel overrides document is invalid, ignoring: %s", exc)
    return CarouselOverrides()


async def save_overrides(overrides: CarouselOverrides) -> None:
    await kv_set(OVERRIDES_KEY, overrides.model_dump_json(), _OVERRIDES_TTL)


# ──────────────────────────────────────────────────────────────────────────
# Merge — file + overrides → what the portal shows / the storefront serves
# ──────────────────────────────────────────────────────────────────────────


def _patched(recipe: RegistryRecipe, patch: dict[str, Any]) -> RegistryRecipe:
    """Apply an operator patch, revalidating the result. A patch that no longer
    validates (e.g. schema tightened since it was saved) is dropped rather than
    breaking the shelf."""
    try:
        return RegistryRecipe.model_validate({**recipe.model_dump(), **patch})
    except ValidationError as exc:
        logger.warning("stale carousel edit for %s ignored: %s", recipe.id, exc)
        return recipe


def merged_registry(overrides: CarouselOverrides) -> list[AdminRecipe]:
    """The full registry with overrides applied — file order first (tombstoned
    entries included, flagged ``removed``), operator-added recipes after."""
    out: list[AdminRecipe] = []
    removed = set(overrides.removed)
    for recipe in FILE_RECIPES:
        patch = overrides.edits.get(recipe.id)
        merged = _patched(recipe, patch) if patch else recipe
        out.append(
            AdminRecipe(
                **merged.model_dump(),
                origin="file",
                edited=bool(patch),
                removed=recipe.id in removed,
            )
        )
    for recipe in overrides.added:
        out.append(AdminRecipe(**recipe.model_dump(), origin="custom"))
    return out


def serves_game(recipe: RegistryRecipe, game: str) -> bool:
    if recipe.games is not None:
        return game in recipe.games
    return game in PRICED_GAMES


def _as_recipe(recipe: RegistryRecipe) -> CarouselRecipe:
    """Down-cast a registry entry to the wire ``CarouselRecipe`` shape."""
    return CarouselRecipe.model_validate(
        recipe.model_dump(include=set(CarouselRecipe.model_fields))
    )


def recipes_for(game: str, overrides: CarouselOverrides) -> list[CarouselRecipe]:
    """The enabled, game-scoped recipes to serve — the live curated pool."""
    return [
        _as_recipe(r)
        for r in merged_registry(overrides)
        if r.enabled and not r.removed and serves_game(r, game)
    ]


# ──────────────────────────────────────────────────────────────────────────
# Operator operations (each: load → mutate → save → return the merged view)
# ──────────────────────────────────────────────────────────────────────────


class UnknownRecipeError(KeyError):
    """No recipe with that id exists in the file registry or the overrides."""


class DuplicateRecipeError(ValueError):
    """A recipe with that id already exists."""


def _find_added(overrides: CarouselOverrides, recipe_id: str) -> RegistryRecipe | None:
    return next((r for r in overrides.added if r.id == recipe_id), None)


def _admin_view(overrides: CarouselOverrides, recipe_id: str) -> AdminRecipe:
    view = next((r for r in merged_registry(overrides) if r.id == recipe_id), None)
    if view is None:  # pragma: no cover - callers validate existence first
        raise UnknownRecipeError(recipe_id)
    return view


async def update_recipe(recipe_id: str, patch: dict[str, Any]) -> AdminRecipe:
    """Patch one recipe (toggle/edit). File recipes accumulate a diff-only
    patch (fields equal to the file value are pruned, so reverting by hand
    clears the "edited" badge); custom recipes are updated in place."""
    overrides = await get_overrides()
    file_recipe = _FILE_BY_ID.get(recipe_id)
    if file_recipe is not None:
        merged = {**overrides.edits.get(recipe_id, {}), **patch}
        # Validate the patched result before persisting anything.
        patched = RegistryRecipe.model_validate({**file_recipe.model_dump(), **merged})
        file_dump = file_recipe.model_dump()
        diff = {k: v for k, v in patched.model_dump().items() if v != file_dump.get(k)}
        if diff:
            overrides.edits[recipe_id] = diff
        else:
            overrides.edits.pop(recipe_id, None)
    else:
        added = _find_added(overrides, recipe_id)
        if added is None:
            raise UnknownRecipeError(recipe_id)
        updated = RegistryRecipe.model_validate({**added.model_dump(), **patch})
        overrides.added = [updated if r.id == recipe_id else r for r in overrides.added]
    await save_overrides(overrides)
    return _admin_view(overrides, recipe_id)


async def add_recipe(recipe: RegistryRecipe) -> AdminRecipe:
    """Add an operator-authored recipe (must not collide with any live id)."""
    overrides = await get_overrides()
    exists_in_file = recipe.id in _FILE_BY_ID
    if exists_in_file or _find_added(overrides, recipe.id) is not None:
        raise DuplicateRecipeError(recipe.id)
    overrides.added.append(recipe)
    await save_overrides(overrides)
    return _admin_view(overrides, recipe.id)


async def delete_recipe(recipe_id: str) -> None:
    """Delete a recipe: custom ones are dropped outright; file ones are
    tombstoned (still listed in the portal, restorable via reset)."""
    overrides = await get_overrides()
    if recipe_id in _FILE_BY_ID:
        if recipe_id not in overrides.removed:
            overrides.removed.append(recipe_id)
    elif _find_added(overrides, recipe_id) is not None:
        overrides.added = [r for r in overrides.added if r.id != recipe_id]
    else:
        raise UnknownRecipeError(recipe_id)
    await save_overrides(overrides)


async def reset_recipe(recipe_id: str) -> AdminRecipe:
    """Restore a file recipe to its checked-in state (clears edits + tombstone).
    Only meaningful for file recipes — custom ones have nothing to reset to."""
    if recipe_id not in _FILE_BY_ID:
        raise UnknownRecipeError(recipe_id)
    overrides = await get_overrides()
    overrides.edits.pop(recipe_id, None)
    overrides.removed = [r for r in overrides.removed if r != recipe_id]
    await save_overrides(overrides)
    return _admin_view(overrides, recipe_id)


async def set_ai_enabled(enabled: bool) -> CarouselOverrides:
    """The AI-shelf kill switch: off = every game serves the (operator-merged)
    curated registry, and no model calls are made."""
    overrides = await get_overrides()
    overrides.aiEnabled = enabled
    await save_overrides(overrides)
    return overrides


__all__ = [
    "CATALOG_ONLY_GAMES",
    "FILE_RECIPES",
    "GAME_LABELS",
    "OVERRIDES_KEY",
    "PRICED_GAMES",
    "DuplicateRecipeError",
    "UnknownRecipeError",
    "add_recipe",
    "delete_recipe",
    "get_overrides",
    "merged_registry",
    "recipes_for",
    "reset_recipe",
    "save_overrides",
    "serves_game",
    "set_ai_enabled",
    "update_recipe",
]
