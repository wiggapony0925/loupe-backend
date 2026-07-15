"""Model transport for Loupe AI — OpenAI preferred, Anthropic fallback.

The one place that talks to a model vendor. Everything above this module
deals in prompts-in / text-out, so swapping or adding a provider never
touches orchestration, schemas, or routes.
"""

from __future__ import annotations

from app.config import get_settings
from app.services.ai.config import PLAN_MAX_TOKENS


def configured() -> bool:
    """Whether ANY model provider has a key set."""
    s = get_settings()
    return bool(s.openai_api_key or s.anthropic_api_key)


async def ask(system: str, user: str) -> str | None:
    """One model call via whichever provider is configured.

    Returns the raw text answer, or ``None`` when no provider has a key.
    Provider/network errors propagate — the orchestrator turns them into a
    graceful fallback (it must decide, not us).
    """
    settings = get_settings()
    if settings.openai_api_key:
        return await _ask_openai(system, user)
    if settings.anthropic_api_key:
        return await _ask_anthropic(system, user)
    return None


async def _ask_openai(system: str, user: str) -> str:
    from openai import AsyncOpenAI

    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.chat.completions.create(
        model=settings.carousel_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0,  # deterministic → cacheable across users
        max_tokens=PLAN_MAX_TOKENS,
    )
    return resp.choices[0].message.content or ""


async def _ask_anthropic(system: str, user: str) -> str:
    from anthropic import AsyncAnthropic

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=settings.nl_query_model,
        max_tokens=PLAN_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


__all__ = ["ask", "configured"]
