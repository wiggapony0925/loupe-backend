"""Schemas for the admin Operations surface — health, database, cloud, audit.

These power the read-only observability pages in the developer portal. They
deliberately carry *status and shape*, never secret values (keys are reported
as present/absent, never echoed).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

CheckStatus = Literal["ok", "warn", "down", "unconfigured"]
OverallStatus = Literal["ok", "warn", "down"]


# ── System health ──────────────────────────────────────────────────────────
class HealthCheck(BaseModel):
    """One probe in the health report (DB, migrations, Redis, a provider, …)."""

    key: str
    label: str
    status: CheckStatus
    detail: str
    # Grouping for the UI: "core" | "data" | "infra" | "config".
    category: str


class HealthReport(BaseModel):
    """Aggregate health — the worst check sets the headline status."""

    status: OverallStatus
    generated_at: datetime
    checks: list[HealthCheck]


# ── Database explorer (metadata only — no row data) ─────────────────────────
class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool
    primary_key: bool
    # "table.column" when this column is a foreign key, else None.
    foreign_key: str | None = None


class IndexInfo(BaseModel):
    name: str
    columns: list[str]
    unique: bool


class ForeignKeyInfo(BaseModel):
    columns: list[str]
    references_table: str
    references_columns: list[str]


class TableSummary(BaseModel):
    name: str
    columns: int
    row_estimate: int
    foreign_keys: int


class TableDetail(BaseModel):
    name: str
    row_estimate: int
    columns: list[ColumnInfo]
    indexes: list[IndexInfo]
    foreign_keys: list[ForeignKeyInfo]
    # Tables that hold a foreign key pointing at this one.
    referenced_by: list[str]


class DatabaseOverview(BaseModel):
    dialect: str
    table_count: int
    tables: list[TableSummary]


class SchemaGraphNode(BaseModel):
    table: str
    columns: int


class SchemaGraphEdge(BaseModel):
    # `source` holds the foreign key that points at `target`.
    source: str
    target: str
    label: str


class SchemaGraph(BaseModel):
    nodes: list[SchemaGraphNode]
    edges: list[SchemaGraphEdge]


# ── Google Cloud (read-only) ────────────────────────────────────────────────
class CloudService(BaseModel):
    name: str
    status: Literal["ready", "deploying", "error", "unknown"]
    revision: str | None = None
    image: str | None = None
    commit_sha: str | None = None
    region: str | None = None
    url: str | None = None
    updated_at: datetime | None = None


class CloudSqlInstance(BaseModel):
    name: str
    state: str
    region: str | None = None


class CloudLogEntry(BaseModel):
    timestamp: datetime
    severity: str
    service: str | None = None
    message: str


class CloudStatus(BaseModel):
    configured: bool
    project_id: str | None = None
    region: str | None = None
    detail: str
    services: list[CloudService]
    sql_instances: list[CloudSqlInstance]


# ── Audit log viewer ────────────────────────────────────────────────────────
class AuditEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None = None
    actor_email: str | None = None
    action: str
    target_table: str | None = None
    target_id: str | None = None
    payload: dict | None = None
    ip_address: str | None = None
    created_at: datetime


class AuditPage(BaseModel):
    results: list[AuditEntry]
    total: int
    page: int
    page_size: int


class AuditFacets(BaseModel):
    """Distinct values for the viewer's filter dropdowns."""

    actions: list[str]
    tables: list[str]


__all__ = [
    "AuditEntry",
    "AuditFacets",
    "AuditPage",
    "CloudLogEntry",
    "CloudService",
    "CloudSqlInstance",
    "CloudStatus",
    "ColumnInfo",
    "DatabaseOverview",
    "ForeignKeyInfo",
    "HealthCheck",
    "HealthReport",
    "IndexInfo",
    "SchemaGraph",
    "SchemaGraphEdge",
    "SchemaGraphNode",
    "TableDetail",
    "TableSummary",
]
