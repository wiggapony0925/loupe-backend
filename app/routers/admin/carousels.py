"""Admin carousel registry management (`/v1/admin/carousels`).

Live operator control over every marketplace carousel: the checked-in JSON
registry merged with the operator's kv_cache overrides (toggle/edit/add/delete
— no deploy, no migration), the latest AI-designed shelves per game, an AI
kill switch, and a force-regenerate button. Every mutation purges the cached
resolved payloads so the storefront reflects the edit on the next load.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.schemas.carousel import (
    AdminCarouselsView,
    AdminRecipe,
    CarouselRecipe,
    CarouselResponse,
    GameCarouselSummary,
    RecipeUpdate,
    RegistryRecipe,
)
from app.services import audit_service
from app.services.catalog import carousel_registry, carousel_service
from app.services.catalog.carousel_registry import (
    CATALOG_ONLY_GAMES,
    GAME_LABELS,
    DuplicateRecipeError,
    UnknownRecipeError,
)

router = APIRouter(prefix="/carousels", tags=["admin-carousels"])


class AiToggle(BaseModel):
    enabled: bool


@router.get(
    "",
    response_model=AdminCarouselsView,
    summary="Carousel registry — file + live overrides + latest AI shelves",
)
async def carousels_overview() -> AdminCarouselsView:
    overrides = await carousel_registry.get_overrides()
    recipes = carousel_registry.merged_registry(overrides)

    games: list[GameCarouselSummary] = []
    ai: dict[str, list[CarouselRecipe]] = {}
    for game, label in GAME_LABELS.items():
        ai_resp = await carousel_service.cached_ai(game)
        if ai_resp is not None:
            ai[game] = ai_resp.carousels
        resolved = await carousel_service.cached_resolved(game)
        games.append(
            GameCarouselSummary(
                id=game,
                label=label,
                catalogOnly=game in CATALOG_ONLY_GAMES,
                curatedCount=len(carousel_registry.recipes_for(game, overrides)),
                aiCount=len(ai_resp.carousels) if ai_resp is not None else None,
                activeSource=(
                    "ai" if overrides.aiEnabled and ai_resp is not None else "curated"
                ),
                resolvedRails=len(resolved.rails) if resolved is not None else None,
            )
        )

    return AdminCarouselsView(
        aiConfigured=carousel_service.configured(),
        aiEnabled=overrides.aiEnabled,
        recipes=recipes,
        games=games,
        ai=ai,
    )


@router.put(
    "/ai",
    response_model=AdminCarouselsView,
    summary="AI kill switch — off pins every game to the curated registry",
)
async def set_ai_enabled(
    payload: AiToggle,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> AdminCarouselsView:
    await carousel_registry.set_ai_enabled(payload.enabled)
    await carousel_service.purge_resolved()
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="carousel.ai_toggle",
        target_table="kv_cache",
        target_id=carousel_registry.OVERRIDES_KEY,
        payload={"enabled": payload.enabled},
    )
    return await carousels_overview()


@router.post(
    "/regenerate",
    response_model=CarouselResponse,
    summary="Force a fresh AI design pass for a game (synchronous)",
)
async def regenerate(
    request: Request,
    game: str = Query(..., description="Game key, e.g. pokemon"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> CarouselResponse:
    game = game.lower()
    if game not in GAME_LABELS:
        raise HTTPException(status_code=404, detail=f"Unknown game '{game}'.")
    if not carousel_service.configured():
        raise HTTPException(
            status_code=400,
            detail="No AI model configured — set OPENAI_API_KEY (or ANTHROPIC_API_KEY).",
        )
    try:
        resp = await carousel_service.regenerate_ai(game)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="carousel.regenerate",
        target_id=game,
        payload={"shelves": [c.id for c in resp.carousels]},
    )
    return resp


@router.post(
    "",
    response_model=AdminRecipe,
    status_code=status.HTTP_201_CREATED,
    summary="Add an operator-authored carousel recipe",
)
async def create_recipe(
    payload: RegistryRecipe,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> AdminRecipe:
    try:
        view = await carousel_registry.add_recipe(payload)
    except DuplicateRecipeError as exc:
        raise HTTPException(
            status_code=409, detail=f"A recipe with id '{payload.id}' already exists."
        ) from exc
    await carousel_service.purge_resolved()
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="carousel.create",
        target_id=payload.id,
        payload=payload.model_dump(exclude_none=True),
    )
    return view


@router.put(
    "/{recipe_id}",
    response_model=AdminRecipe,
    summary="Toggle / edit one recipe (partial update)",
)
async def update_recipe(
    recipe_id: str,
    payload: RecipeUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> AdminRecipe:
    patch = payload.model_dump(exclude_unset=True)
    try:
        view = await carousel_registry.update_recipe(recipe_id, patch)
    except UnknownRecipeError as exc:
        raise HTTPException(
            status_code=404, detail=f"No recipe with id '{recipe_id}'."
        ) from exc
    await carousel_service.purge_resolved()
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="carousel.update",
        target_id=recipe_id,
        payload=patch,
    )
    return view


@router.post(
    "/{recipe_id}/reset",
    response_model=AdminRecipe,
    summary="Restore a file recipe to its checked-in state",
)
async def reset_recipe(
    recipe_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> AdminRecipe:
    try:
        view = await carousel_registry.reset_recipe(recipe_id)
    except UnknownRecipeError as exc:
        raise HTTPException(
            status_code=404, detail=f"No file recipe with id '{recipe_id}'."
        ) from exc
    await carousel_service.purge_resolved()
    await audit_service.record(
        db, request=request, user=user, action="carousel.reset", target_id=recipe_id
    )
    return view


@router.delete(
    "/{recipe_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a recipe (file recipes are tombstoned, restorable via reset)",
)
async def delete_recipe(
    recipe_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> None:
    try:
        await carousel_registry.delete_recipe(recipe_id)
    except UnknownRecipeError as exc:
        raise HTTPException(
            status_code=404, detail=f"No recipe with id '{recipe_id}'."
        ) from exc
    await carousel_service.purge_resolved()
    await audit_service.record(
        db, request=request, user=user, action="carousel.delete", target_id=recipe_id
    )


__all__ = ["router"]
