"""Upload every exported email template to Resend's hosted templates.

Drives the official ``resend-cli`` (create as draft → publish), matching each
template by its ``loupe-*`` alias. Needs a FULL-ACCESS Resend API key (a
"sending only" key 401s on template management):

    RESEND_API_KEY=re_xxx .venv/bin/python scripts/upload_email_templates.py

Run ``scripts/export_email_templates.py`` first to refresh ``dist/``. The
codebase remains the source of truth — this syncs a browsable copy to the
Resend dashboard. Template ids land in ``dist/resend-templates.json``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

DIST = Path(__file__).resolve().parents[1] / "app/services/email_templates/dist"


def _run(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["resend", *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _extract_id(output: str) -> str | None:
    try:
        data = json.loads(output)
    except ValueError:
        return None
    if isinstance(data, dict):
        for candidate in (data, data.get("data") or {}):
            if isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
                return candidate["id"]
    return None


def _existing_by_alias() -> dict[str, str]:
    """alias → template id for everything already in the Resend account."""
    found: dict[str, str] = {}
    cursor: str | None = None
    while True:
        args = ["templates", "list", "--limit", "100"]
        if cursor:
            args += ["--after", cursor]
        code, out = _run(*args)
        if code != 0:
            break
        try:
            page = json.loads(out)
        except ValueError:
            break
        rows = page.get("data") or []
        for row in rows:
            if row.get("alias"):
                found[row["alias"]] = row["id"]
        if not page.get("has_more") or not rows:
            break
        cursor = rows[-1]["id"]
    return found


def main() -> int:
    if not os.environ.get("RESEND_API_KEY"):
        print("Set RESEND_API_KEY (full-access key) in the environment.")
        return 1
    manifest = json.loads((DIST / "manifest.json").read_text())
    # Aliases are unique account-wide and there's no update command — replace:
    # delete the previous upload for each alias, then create + publish fresh.
    existing = _existing_by_alias()
    results = []
    failures = 0
    for entry in manifest:
        name = entry["name"]
        if name in existing:
            _run("templates", "delete", existing[name], "--yes")
        code, out = _run(
            "templates",
            "create",
            "--name",
            name,
            "--alias",
            name,
            "--html-file",
            str(DIST / entry["html"]),
            "--text-file",
            str(DIST / entry["text"]),
            "--subject",
            entry["subject"],
        )
        template_id = _extract_id(out)
        if code != 0 or not template_id:
            failures += 1
            print(f"✗ {name}: {out.strip()[:160]}")
            continue
        pcode, pout = _run("templates", "publish", template_id)
        published = pcode == 0
        if not published:
            print(f"  (created but publish failed: {pout.strip()[:120]})")
        results.append(
            {
                "key": entry["key"],
                "name": name,
                "id": template_id,
                "published": published,
            }
        )
        print(f"✓ {name} → {template_id}{' (published)' if published else ''}")
    (DIST / "resend-templates.json").write_text(json.dumps(results, indent=2) + "\n")
    print(f"\n{len(results)} uploaded, {failures} failed → dist/resend-templates.json")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
