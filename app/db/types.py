"""Cross-dialect column types.

Postgres exposes rich types (UUID, JSONB, INET, NUMERIC); the test suite
uses SQLite via aiosqlite which lacks them. SQLAlchemy 2.0's :class:`Uuid`
and :class:`JSON` types transparently emit the right DDL per dialect.
"""

from __future__ import annotations

from sqlalchemy import JSON, Uuid

# JsonCol maps to JSONB on Postgres and JSON (TEXT) on SQLite.
JsonCol = JSON

# UuidCol uses Postgres UUID natively and TEXT on SQLite.
UuidCol = Uuid

__all__ = ["JsonCol", "UuidCol"]
