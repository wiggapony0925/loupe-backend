"""Real card-data provider layer (eBay, PSA, TCGplayer, PriceCharting, 130point, GoCollect).

Each provider is *env-gated*: when its credentials are missing, methods
return empty lists / ``None`` so the rest of the backend degrades to the
existing synthesized fallback. See :class:`app.integrations.base.BaseProvider`.
"""

from app.integrations.base import (
    BaseProvider,
    Listing,
    MarketPrice,
    PopulationReport,
    SoldComp,
    close_http_client,
)
from app.integrations.registry import ProviderRegistry, get_registry

__all__ = [
    "BaseProvider",
    "Listing",
    "MarketPrice",
    "PopulationReport",
    "ProviderRegistry",
    "SoldComp",
    "close_http_client",
    "get_registry",
]
