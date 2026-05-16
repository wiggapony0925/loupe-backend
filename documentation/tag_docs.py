"""Programmatic per-tag descriptions for the OpenAPI doc."""

from __future__ import annotations

#: Tag → human-friendly summary shown in the OpenAPI sidebar.
TAG_DESCRIPTIONS: dict[str, str] = {
    "system": "Health, version, and metrics endpoints. Unauthenticated.",
    "auth": "Sign-In with Apple/Google exchange and refresh-token rotation.",
    "users": "Authenticated profile + per-user settings.",
    "scanners": "Hardware scanner pairing, heartbeat, and CRUD.",
    "scans": "Scan-job ingestion (presigned uploads) + grading triggers.",
    "cards": "Read-only card catalog search & lookup.",
    "sets": "Read-only card-set browsing (paginated).",
    "grades": "User's graded-card history (soft-deletable).",
    "prices": "Historical price snapshots per card/grade/house.",
    "collections": "User-defined binders/decks with graded-card membership.",
    "ws": "WebSocket channels (live scan progress, future notifications).",
}


def render_tag_matrix() -> str:
    """Return a markdown table summarising every tag for the OpenAPI doc."""
    lines = ["| Tag | Description |", "| --- | --- |"]
    for tag, desc in sorted(TAG_DESCRIPTIONS.items()):
        lines.append(f"| `{tag}` | {desc} |")
    return "\n".join(lines)


__all__ = ["TAG_DESCRIPTIONS", "render_tag_matrix"]
