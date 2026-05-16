#!/usr/bin/env python3
"""Local development entry point.

Auto-detects a `.venv` virtual environment and re-execs into it if needed,
then launches `uvicorn` with `--reload`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _venv_python() -> Path | None:
    """Return the path to .venv/bin/python if it exists."""
    candidate = ROOT / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else None


def main() -> int:
    venv_py = _venv_python()
    if venv_py and Path(sys.executable).resolve() != venv_py.resolve():
        os.execv(
            str(venv_py), [str(venv_py), str(Path(__file__).resolve()), *sys.argv[1:]]
        )

    host = os.environ.get("HOST", "0.0.0.0")
    port = os.environ.get("PORT", "8000")
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        port,
        "--reload",
        "--no-access-log",
    ]
    print(f"🚀 starting loupe-backend → http://{host}:{port}")
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        print("\n👋 shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())
