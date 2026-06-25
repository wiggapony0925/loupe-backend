"""Read-only Google Cloud connections for the developer portal.

Reports what's deployed where — Cloud Run services with their live revision,
image, and commit SHA — plus a recent-log tail and the configured Cloud SQL
instance. Strictly read-only: it uses Viewer-level client libraries and never
mutates infrastructure.

Degrades gracefully. With the optional ``google-cloud-run`` /
``google-cloud-logging`` libraries absent, or with no project resolvable, every
call returns a clear ``configured=False`` status instead of erroring — the same
philosophy as the rest of the optional integrations in :mod:`app.config`.

The project id is resolved automatically: an explicit ``GCP_PROJECT_ID`` wins,
otherwise it's taken from the Cloud SQL connection name, otherwise from
Application Default Credentials — so on Cloud Run the panel lights up with no
extra configuration beyond granting the service account Viewer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.config import Settings, get_settings
from app.schemas.ops import (
    CloudLogEntry,
    CloudService,
    CloudSqlInstance,
    CloudStatus,
)
from app.utils.logger import get_logger

logger = get_logger("admin.gcp")

# Cloud Run services we expect to find, in display order. Discovery lists
# whatever exists; this just gives a stable, friendly ordering.
_KNOWN_SERVICES = ("loupe-api", "loupe-web", "loupe-worker")

# Cloud Run v2 Condition.State → our coarse status. SUCCEEDED is the healthy
# state; PENDING/RECONCILING mean a rollout is in flight; FAILED is an error.
_CONDITION_STATE = {
    1: "deploying",  # CONDITION_PENDING
    2: "deploying",  # CONDITION_RECONCILING
    3: "error",  # CONDITION_FAILED
    4: "ready",  # CONDITION_SUCCEEDED
}


def _project_id(settings: Settings) -> str | None:
    """Resolve the GCP project: explicit env → Cloud SQL name → ADC."""
    if settings.gcp_project_id:
        return settings.gcp_project_id
    # cloud_sql_connection_name is "project:region:instance".
    if settings.cloud_sql_connection_name and ":" in settings.cloud_sql_connection_name:
        return settings.cloud_sql_connection_name.split(":", 1)[0]
    try:
        import google.auth

        _, project = google.auth.default()
        return project
    except Exception:
        return None


def _unconfigured(detail: str) -> CloudStatus:
    settings = get_settings()
    return CloudStatus(
        configured=False,
        project_id=_project_id(settings),
        region=settings.gcp_region,
        detail=detail,
        services=[],
        sql_instances=_sql_from_config(settings),
    )


def _sql_from_config(settings: Settings) -> list[CloudSqlInstance]:
    """Cloud SQL surfaced from config (connection name), not a live admin call."""
    name = settings.cloud_sql_connection_name
    if not name:
        return []
    # "project:region:instance" — show the instance + region without a call.
    parts = name.split(":")
    return [
        CloudSqlInstance(
            name=parts[-1],
            state="configured",
            region=parts[1] if len(parts) >= 3 else settings.gcp_region,
        )
    ]


async def status() -> CloudStatus:
    """Live Cloud Run + Cloud SQL status, or a graceful unconfigured report."""
    settings = get_settings()
    project = _project_id(settings)
    if not project:
        return _unconfigured(
            "No GCP project resolved. Set GCP_PROJECT_ID (and grant the service "
            "account Viewer) to enable."
        )
    try:
        return await asyncio.to_thread(_load_status_sync, settings, project)
    except ImportError:
        return _unconfigured(
            "google-cloud-run is not installed in this image — add it to enable."
        )
    except Exception as exc:
        logger.warning("GCP status fetch failed: %s", exc)
        return _unconfigured(
            f"Could not reach Cloud Run ({type(exc).__name__}). Check the service "
            "account has run.viewer + logging.viewer."
        )


def _service_status(svc: object) -> str:
    """Coarse status from a Service's terminal/ready condition."""
    cond = getattr(svc, "terminal_condition", None)
    if cond is None or getattr(cond, "type_", None) != "Ready":
        cond = next(
            (c for c in getattr(svc, "conditions", []) if c.type_ == "Ready"), cond
        )
    if cond is None:
        return "unknown"
    return _CONDITION_STATE.get(int(cond.state), "unknown")


def _load_status_sync(settings: Settings, project: str) -> CloudStatus:
    """Blocking Cloud Run listing — run in a thread by :func:`status`."""
    from google.cloud import run_v2

    client = run_v2.ServicesClient()
    parent = f"projects/{project}/locations/{settings.gcp_region}"
    services: list[CloudService] = []
    for svc in client.list_services(parent=parent):
        short = svc.name.rsplit("/", 1)[-1]
        template = svc.template
        image = template.containers[0].image if template.containers else None
        labels = dict(svc.labels)
        services.append(
            CloudService(
                name=short,
                status=_service_status(svc),  # type: ignore[arg-type]
                revision=(svc.latest_ready_revision or "").rsplit("/", 1)[-1] or None,
                image=image,
                commit_sha=labels.get("commit-sha")
                or labels.get("commit_sha")
                or labels.get("commit"),
                region=settings.gcp_region,
                url=svc.uri or None,
                updated_at=svc.update_time,
            )
        )
    services.sort(
        key=lambda s: _KNOWN_SERVICES.index(s.name)
        if s.name in _KNOWN_SERVICES
        else len(_KNOWN_SERVICES)
    )
    return CloudStatus(
        configured=True,
        project_id=project,
        region=settings.gcp_region,
        detail=f"{len(services)} Cloud Run service(s) in {settings.gcp_region}.",
        services=services,
        sql_instances=_sql_from_config(settings),
    )


async def recent_logs(limit: int = 25) -> list[CloudLogEntry]:
    """Most recent Cloud Run log entries across services (newest first)."""
    settings = get_settings()
    project = _project_id(settings)
    if not project:
        return []
    try:
        return await asyncio.to_thread(_load_logs_sync, project, limit)
    except ImportError:
        return []
    except Exception as exc:
        logger.warning("GCP log fetch failed: %s", exc)
        return []


def _load_logs_sync(project: str, limit: int) -> list[CloudLogEntry]:
    """Blocking Cloud Logging tail — run in a thread by :func:`recent_logs`."""
    from google.cloud import logging_v2

    client = logging_v2.Client(project=project)
    log_filter = 'resource.type="cloud_run_revision"'
    entries: list[CloudLogEntry] = []
    for entry in client.list_entries(
        filter_=log_filter, order_by=logging_v2.DESCENDING, max_results=limit
    ):
        payload = entry.payload
        message = payload if isinstance(payload, str) else str(payload)
        resource_labels = getattr(entry.resource, "labels", {}) or {}
        entries.append(
            CloudLogEntry(
                timestamp=entry.timestamp or datetime.now(),
                severity=str(entry.severity or "DEFAULT"),
                service=resource_labels.get("service_name"),
                message=message[:1000],
            )
        )
    return entries


__all__ = ["recent_logs", "status"]
