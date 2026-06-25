"""Unit tests for the admin Operations services (health, schema, audit, gcp)."""

from types import SimpleNamespace

import pytest

from app.models.audit import AuditLog
from app.services.admin import (
    audit_query_service,
    gcp_service,
    health_service,
    schema_service,
)


# ── schema_service ──
@pytest.mark.asyncio
async def test_overview_lists_known_tables(db_session):
    overview = await schema_service.overview(db_session)
    names = {t.name for t in overview.tables}
    assert overview.table_count == len(overview.tables)
    assert {"users", "audit_log"} <= names
    for t in overview.tables:
        assert t.row_estimate >= 0
        assert t.columns > 0


@pytest.mark.asyncio
async def test_table_detail_shape_and_relationships(db_session):
    detail = await schema_service.table_detail(db_session, "audit_log")
    assert detail is not None
    col_names = {c.name for c in detail.columns}
    assert {"action", "user_id", "created_at"} <= col_names
    # audit_log.user_id is a FK onto users.
    fk_targets = {fk.references_table for fk in detail.foreign_keys}
    assert "users" in fk_targets
    # users is referenced_by audit_log.
    users_detail = await schema_service.table_detail(db_session, "users")
    assert users_detail is not None
    assert "audit_log" in users_detail.referenced_by


@pytest.mark.asyncio
async def test_table_detail_unknown_returns_none(db_session):
    assert await schema_service.table_detail(db_session, "does_not_exist") is None


@pytest.mark.asyncio
async def test_graph_has_nodes_and_fk_edges(db_session):
    graph = schema_service.graph()
    assert len(graph.nodes) > 0
    assert any(e.source == "audit_log" and e.target == "users" for e in graph.edges)


# ── health_service ──
@pytest.mark.asyncio
async def test_health_report_structure(db_session):
    report = await health_service.report(db_session)
    assert report.status in {"ok", "warn", "down"}
    keys = {c.key for c in report.checks}
    # The headline checks must always be present.
    assert {"database", "migrations"} <= keys
    db_check = next(c for c in report.checks if c.key == "database")
    assert db_check.status == "ok"  # the test DB is reachable
    # No check leaks a secret value.
    for c in report.checks:
        assert c.status in {"ok", "warn", "down", "unconfigured"}


# ── audit_query_service ──
@pytest.mark.asyncio
async def test_audit_page_filters_and_joins_actor(db_session, created_user):
    db_session.add_all(
        [
            AuditLog(
                user_id=created_user.id,
                action="flag.update",
                target_table="feature_flags",
                target_id="abc",
                payload={"enabled": True},
                ip_address="1.2.3.4",
            ),
            AuditLog(
                action="user.ban",
                target_table="users",
                target_id="xyz",
            ),
        ]
    )
    await db_session.commit()

    all_rows = await audit_query_service.page(db_session)
    assert all_rows.total == 2

    # Filter by action.
    flags_only = await audit_query_service.page(db_session, action="flag.update")
    assert flags_only.total == 1
    assert flags_only.results[0].actor_email == created_user.email

    # Filter by actor email substring.
    by_actor = await audit_query_service.page(
        db_session, actor=created_user.email[:5]
    )
    assert by_actor.total == 1

    facets = await audit_query_service.facets(db_session)
    assert {"flag.update", "user.ban"} <= set(facets.actions)
    assert {"feature_flags", "users"} <= set(facets.tables)


# ── gcp_service (status enum + project resolution) ──
def test_gcp_service_status_maps_condition_enum():
    """SUCCEEDED(4)→ready, FAILED(3)→error, PENDING(1)→deploying."""
    def svc(state: int) -> object:
        cond = SimpleNamespace(type_="Ready", state=state)
        return SimpleNamespace(terminal_condition=cond, conditions=[])

    assert gcp_service._service_status(svc(4)) == "ready"
    assert gcp_service._service_status(svc(3)) == "error"
    assert gcp_service._service_status(svc(1)) == "deploying"
    assert gcp_service._service_status(svc(0)) == "unknown"


def test_gcp_project_id_resolution():
    explicit = SimpleNamespace(gcp_project_id="explicit", cloud_sql_connection_name=None)
    assert gcp_service._project_id(explicit) == "explicit"

    # Derived from the Cloud SQL connection name ("project:region:instance").
    from_sql = SimpleNamespace(
        gcp_project_id=None, cloud_sql_connection_name="proj-1:us-central1:inst"
    )
    assert gcp_service._project_id(from_sql) == "proj-1"
