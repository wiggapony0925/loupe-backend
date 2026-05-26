"""Pure domain types.

This package holds framework-free value objects that are shared across
service / integration / router boundaries. Nothing in here may import
SQLAlchemy, FastAPI, Pydantic models from ``app.schemas``, or any
``app.integrations.*`` module — domain is the bottom of the stack.

Add new shapes here when they need to travel between two layers and
have no natural ORM or wire-schema home.
"""

from app.domain.market import Listing, MarketPrice, PopulationReport, SoldComp

__all__ = ["Listing", "MarketPrice", "PopulationReport", "SoldComp"]
