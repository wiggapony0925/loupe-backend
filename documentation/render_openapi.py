"""Concatenate every markdown fragment + dynamic section into one OpenAPI blurb."""

from __future__ import annotations

from pathlib import Path

from documentation.tag_docs import render_tag_matrix
from documentation.url_docs import render_upstream_urls

_HERE = Path(__file__).resolve().parent

_STATIC_ORDER: tuple[str, ...] = (
    "openapi_overview.md",
    "api_principles.md",
    "data_lifecycle.md",
    "endpoint_playbook.md",
)


def _read(name: str) -> str:
    path = _HERE / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").rstrip() + "\n"


def build_full_description() -> str:
    """Return the assembled markdown shown on the FastAPI docs landing page."""
    parts: list[str] = [_read(name) for name in _STATIC_ORDER]
    parts.append("## Tag Reference\n\n" + render_tag_matrix() + "\n")
    parts.append("## Upstream Services\n\n" + render_upstream_urls() + "\n")
    return "\n".join(p for p in parts if p)


__all__ = ["build_full_description"]
