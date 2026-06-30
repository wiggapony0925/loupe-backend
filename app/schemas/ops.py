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


# ── Environment manager (presence + non-secret values only) ─────────────────
class EnvVar(BaseModel):
    """One environment variable descriptor for the admin env manager.

    Carries *what it's for* and *whether it's set* — and the value only when it
    is safe to expose. Secrets are NEVER echoed: ``value`` stays ``None`` and we
    surface ``length`` (character count) instead, so an admin can confirm a key
    is present without the bytes ever crossing the wire.
    """

    key: str  # the ENV var name, e.g. "STRIPE_SECRET_KEY"
    label: str
    group: str
    # True for keys/tokens/secrets/credentials — value is withheld.
    secret: bool
    # Whether the variable is configured (non-empty / non-default-empty).
    is_set: bool
    # The actual value — populated ONLY for non-secret config. None otherwise.
    value: str | None = None
    # For secrets: the length of the configured value (0 when unset). Lets the
    # UI show "set · 32 chars" without revealing any characters.
    length: int = 0
    description: str
    # Link to the provider's API docs / console, when relevant.
    docs_url: str | None = None


class EnvReport(BaseModel):
    """The server-side environment, grouped and safe to render."""

    app_env: str
    generated_at: datetime
    variables: list[EnvVar]


# ── Integrations (second-party / external services) ─────────────────────────
IntegrationStatus = Literal["live", "down", "ready", "unconfigured"]


class Integration(BaseModel):
    """One external service the backend depends on (a provider/API/platform)."""

    id: str
    name: str
    # "Catalog" | "Pricing & market" | "Payments" | "Email" | "AI" | …
    category: str
    purpose: str
    # Configured = the env credentials it needs are present (or it's keyless).
    configured: bool
    # Capabilities it serves in the app (listings, comps, market_price, …).
    capabilities: list[str] = []
    docs_url: str | None = None
    # live = reachable just now; down = probe failed; ready = configured but not
    # probed; unconfigured = missing credentials.
    status: IntegrationStatus
    # Populated only when a live probe ran.
    http_status: int | None = None
    latency_ms: int | None = None
    detail: str = ""


class IntegrationsReport(BaseModel):
    """Every external dependency, grouped and (optionally) live-probed."""

    generated_at: datetime
    probed: bool
    integrations: list[Integration]


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
    "EnvReport",
    "EnvVar",
    "ForeignKeyInfo",
    "HealthCheck",
    "HealthReport",
    "IndexInfo",
    "Integration",
    "IntegrationsReport",
    "SchemaGraph",
    "SchemaGraphEdge",
    "SchemaGraphNode",
    "TableDetail",
    "TableSummary",
]
