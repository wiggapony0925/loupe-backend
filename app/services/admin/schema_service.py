"""Read-only database introspection for the Supabase-style schema explorer.

Structure (tables, columns, foreign keys, indexes) comes from the ORM's
``Base.metadata`` — authoritative and identical to what the code expects — while
row counts are read live. This surface never returns row *data* and can never
mutate: table names are validated against the known metadata before any count
query, so there is no avenue for arbitrary table access or injection.
"""

from __future__ import annotations

from sqlalchemy import Table, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.db.session import get_engine
from app.schemas.ops import (
    ColumnInfo,
    DatabaseOverview,
    ForeignKeyInfo,
    IndexInfo,
    SchemaGraph,
    SchemaGraphEdge,
    SchemaGraphNode,
    TableDetail,
    TableSummary,
)

# Known tables, keyed by name — the allowlist for every count query.
_TABLES: dict[str, Table] = Base.metadata.tables


def _dialect() -> str:
    return get_engine().dialect.name


async def overview(db: AsyncSession) -> DatabaseOverview:
    """All tables with column/FK counts and a fast live row estimate."""
    dialect = _dialect()
    summaries: list[TableSummary] = []
    for name, table in sorted(_TABLES.items()):
        summaries.append(
            TableSummary(
                name=name,
                columns=len(table.columns),
                row_estimate=await _row_estimate(db, name, dialect),
                foreign_keys=len(table.foreign_key_constraints),
            )
        )
    return DatabaseOverview(
        dialect=dialect, table_count=len(summaries), tables=summaries
    )


async def table_detail(db: AsyncSession, name: str) -> TableDetail | None:
    """Full structure for one table, or None when the name is unknown."""
    table = _TABLES.get(name)
    if table is None:
        return None

    columns = [
        ColumnInfo(
            name=col.name,
            type=str(col.type),
            nullable=bool(col.nullable),
            primary_key=bool(col.primary_key),
            foreign_key=next(
                (fk.target_fullname for fk in col.foreign_keys), None
            ),
        )
        for col in table.columns
    ]
    indexes = [
        IndexInfo(
            name=ix.name or "",
            columns=[c.name for c in ix.columns],
            unique=bool(ix.unique),
        )
        for ix in table.indexes
    ]
    foreign_keys = [
        ForeignKeyInfo(
            columns=[c.name for c in fk.columns],
            references_table=fk.referred_table.name,
            references_columns=[e.column.name for e in fk.elements],
        )
        for fk in table.foreign_key_constraints
    ]
    referenced_by = sorted(
        other.name
        for other in _TABLES.values()
        if any(
            fk.referred_table is table for fk in other.foreign_key_constraints
        )
    )
    return TableDetail(
        name=name,
        row_estimate=await _row_exact(db, name),
        columns=columns,
        indexes=indexes,
        foreign_keys=foreign_keys,
        referenced_by=referenced_by,
    )


def graph() -> SchemaGraph:
    """Pure-metadata node/edge graph of foreign-key relationships."""
    nodes = [
        SchemaGraphNode(table=name, columns=len(table.columns))
        for name, table in sorted(_TABLES.items())
    ]
    edges: list[SchemaGraphEdge] = []
    for name, table in sorted(_TABLES.items()):
        for fk in table.foreign_key_constraints:
            edges.append(
                SchemaGraphEdge(
                    source=name,
                    target=fk.referred_table.name,
                    label=", ".join(c.name for c in fk.columns),
                )
            )
    return SchemaGraph(nodes=nodes, edges=edges)


async def _row_estimate(db: AsyncSession, name: str, dialect: str) -> int:
    """Fast row estimate — planner stats on Postgres, exact count elsewhere."""
    if dialect == "postgresql":
        result = await db.execute(
            text("SELECT reltuples::bigint FROM pg_class WHERE relname = :t"),
            {"t": name},
        )
        estimate = result.scalar()
        if estimate is not None and estimate >= 0:
            return int(estimate)
    return await _row_exact(db, name)


async def _row_exact(db: AsyncSession, name: str) -> int:
    """Exact count for a single known table (name validated by caller)."""
    table = _TABLES[name]  # KeyError is impossible: callers validate first
    result = await db.execute(select(func.count()).select_from(table))
    return int(result.scalar_one())


__all__ = ["graph", "overview", "table_detail"]
