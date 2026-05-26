"""Dump the FastAPI OpenAPI schema to a file.

Usage::

    python scripts/dump_openapi.py [output.json]

Default output path is ``openapi.json`` in the repo root. Used by the
frontend codegen pipeline (see ``loupe-frontend/scripts/generate-api-
types.sh``) to produce typed wire models without booting the server.

The script monkey-patches the docs perimeter so we don't need auth to
read the schema — we're calling ``app.openapi()`` directly, which is
synchronous and bypasses the router.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the project root (which contains the ``app`` package) is on
# ``sys.path`` regardless of CWD. Running ``python scripts/dump_openapi
# .py`` from the backend dir would otherwise put ``scripts/`` first and
# shadow the import.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main(argv: list[str]) -> int:
    out_path = Path(argv[1]) if len(argv) > 1 else Path("openapi.json")
    # Importing app.main triggers the full app factory; safe because the
    # observability bootstraps are no-ops without DSNs in dev/test.
    from app.main import app

    schema = app.openapi()
    out_path.write_text(json.dumps(schema, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {len(json.dumps(schema))} bytes → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
