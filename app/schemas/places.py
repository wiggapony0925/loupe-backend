"""Place suggestions for the profile location picker."""

from __future__ import annotations

from pydantic import BaseModel


class PlaceSuggestion(BaseModel):
    """One pickable place.

    ``label`` is the string the client renders and STORES — formatting is
    server-owned so the same city never reads two ways across surfaces.
    """

    label: str
    city: str | None = None
    region: str | None = None
    country: str | None = None
    country_code: str | None = None


class PlaceSuggestions(BaseModel):
    places: list[PlaceSuggestion] = []
    #: True when the gazetteer was unreachable. The client keeps letting the
    #: user save what they typed rather than blocking on a lookup.
    degraded: bool = False


__all__ = ["PlaceSuggestion", "PlaceSuggestions"]
