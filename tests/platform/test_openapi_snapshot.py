"""OpenAPI schema snapshot test.

Pins the exact set of paths + their methods exposed by the API so any
intentional addition / removal / rename surfaces as a failing test
that the author must update by re-running the snapshot.

To intentionally accept a schema change, run::

    UPDATE_OPENAPI_SNAPSHOT=1 pytest tests/platform/test_openapi_snapshot.py

This catches:
* Accidentally unregistered routers (path disappears).
* Renamed / re-prefixed endpoints (frontend wire client breaks).
* New endpoints landed without a docs + CONTRACT update.

It deliberately does NOT diff request/response schema bodies — that is
the job of ``test_frontend_contract.py``. This file is the index of
**surface area**: which URLs + verbs the backend speaks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

SNAPSHOT_PATH = Path(__file__).resolve().parent / "openapi_paths.snapshot.json"


def _current_paths() -> dict[str, list[str]]:
    """``{path: [METHOD, ...]}`` for every route in the live schema."""
    schema = app.openapi()
    out: dict[str, list[str]] = {}
    for path, ops in (schema.get("paths") or {}).items():
        methods = sorted(
            m.upper()
            for m in ops
            if m.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}
        )
        if methods:
            out[path] = methods
    return dict(sorted(out.items()))


def test_openapi_paths_snapshot() -> None:
    # Force schema rebuild via TestClient lifespan so router registration
    # mirrors a real server.
    with TestClient(app):
        current = _current_paths()

    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT") == "1" or not SNAPSHOT_PATH.exists():
        SNAPSHOT_PATH.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        if not SNAPSHOT_PATH.exists():
            pytest.skip("snapshot created")
        return

    expected = json.loads(SNAPSHOT_PATH.read_text())
    if expected != current:
        added = sorted(set(current) - set(expected))
        removed = sorted(set(expected) - set(current))
        changed = sorted(
            p for p in set(current) & set(expected) if current[p] != expected[p]
        )
        msg = ["OpenAPI surface drifted from snapshot."]
        if added:
            msg.append(f"  ADDED:   {added}")
        if removed:
            msg.append(f"  REMOVED: {removed}")
        for p in changed:
            msg.append(f"  CHANGED: {p}  was={expected[p]}  now={current[p]}")
        msg.append(
            "If intentional, regenerate with: "
            "UPDATE_OPENAPI_SNAPSHOT=1 pytest tests/platform/test_openapi_snapshot.py"
        )
        pytest.fail("\n".join(msg))
