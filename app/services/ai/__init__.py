"""Loupe AI — the AI feature package, one concern per module.

Organized like a standalone AI backend (thin routes → dedicated handler
modules → tests mirroring modules) so new AI features slot in as siblings
instead of growing one mega-service:

* :mod:`config`    — every tuning knob and client-visible limit.
* :mod:`schemas`   — validated model-output shapes (+ clamping).
* :mod:`prompts`   — prompt builders (game-hint aware).
* :mod:`providers` — the model calls (OpenAI preferred, Anthropic fallback).
* :mod:`search`    — the "describe it" search orchestrator.

Routers import THIS package (``from app.services import ai``) and use the
re-exports below; the submodules stay an implementation detail.
"""

from app.services.ai.config import MESSAGE_MAX_CHARS, QUERY_MAX_CHARS
from app.services.ai.providers import configured
from app.services.ai.schemas import AiSearchPlan
from app.services.ai.search import ai_search

__all__ = [
    "MESSAGE_MAX_CHARS",
    "QUERY_MAX_CHARS",
    "AiSearchPlan",
    "ai_search",
    "configured",
]
