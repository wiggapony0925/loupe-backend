"""Wire schemas for ``GET /v1/providers/status``."""

from __future__ import annotations

from pydantic import BaseModel


class ProviderStatus(BaseModel):
    id: str
    name: str
    configured: bool
    capabilities: list[str]


class ProvidersStatusResponse(BaseModel):
    providers: list[ProviderStatus]


__all__ = ["ProviderStatus", "ProvidersStatusResponse"]
