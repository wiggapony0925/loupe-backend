"""Validated shapes for Loupe AI model output.

The model returns JSON; everything here turns that untrusted text into a
typed, clamped :class:`AiSearchPlan` — or ``None``, never an exception.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field, ValidationError

from app.services.ai.config import GAMES, MESSAGE_MAX_CHARS


class AiSearchPlan(BaseModel):
    """The model's (validated) answer: a message + candidate card names."""

    message: str
    game: str | None = None
    #: "card" = one specific card described; "collection" = a set/theme/group.
    intent: str = "card"
    candidates: list[str] = Field(default_factory=list, max_length=5)
    #: Real SET names the collection maps to ("movie promos" → Black Star
    #: Promos) — resolved against the live set catalog, never trusted as-is.
    sets: list[str] = Field(default_factory=list, max_length=3)


def clip_message(message: str) -> str:
    """Clamp to ``MESSAGE_MAX_CHARS`` at a word boundary (with an ellipsis) —
    an over-chatty model must degrade to a shorter message, never to a
    rejected plan."""
    message = message.strip()
    if len(message) <= MESSAGE_MAX_CHARS:
        return message
    clipped = message[: MESSAGE_MAX_CHARS - 1]
    cut = clipped.rfind(" ")
    if cut > MESSAGE_MAX_CHARS // 2:
        clipped = clipped[:cut]
    return clipped.rstrip(" ,;:.") + "…"


def parse_plan(text: str) -> AiSearchPlan | None:
    """Validate the model's JSON (tolerating stray fences), else ``None``."""
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if i == -1 or j == -1:
            return None
        try:
            data = json.loads(text[i : j + 1])
        except json.JSONDecodeError:
            return None
    try:
        plan = AiSearchPlan.model_validate(data)
    except ValidationError:
        return None
    plan.message = clip_message(plan.message)
    if plan.game not in GAMES:
        plan.game = None
    if plan.intent not in ("card", "collection"):
        plan.intent = "card"
    plan.candidates = [c.strip() for c in plan.candidates if c and c.strip()][:5]
    plan.sets = [x.strip() for x in plan.sets if x and x.strip()][:3]
    return plan if plan.candidates and plan.message else None


__all__ = ["AiSearchPlan", "clip_message", "parse_plan"]
