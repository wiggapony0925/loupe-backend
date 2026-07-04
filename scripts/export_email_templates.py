"""Export every email template as standalone HTML/TXT files.

Renders the exact production builders (sample data, same as the /admin/email
gallery) into ``app/services/email_templates/dist/`` — one ``.html`` and
``.txt`` per template plus a ``manifest.json``. These files are what gets
uploaded to Resend's hosted templates (``resend templates create``) and are
handy for eyeballing in a browser or an email-testing tool.

Run from loupe-backend:  .venv/bin/python scripts/export_email_templates.py

The codebase stays the source of truth — re-run this (and re-upload) after
changing a template.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.admin import email_preview_service

DIST = Path(__file__).resolve().parents[1] / "app/services/email_templates/dist"


def main() -> None:
    DIST.mkdir(exist_ok=True)
    manifest = []
    for spec in email_preview_service.TEMPLATES:
        content = spec.render()
        (DIST / f"{spec.key}.html").write_text(content.html)
        (DIST / f"{spec.key}.txt").write_text(content.text)
        manifest.append(
            {
                "key": spec.key,
                "name": f"loupe-{spec.key.replace('_', '-')}",
                "label": spec.label,
                "group": spec.group,
                "subject": content.subject,
                "html": f"{spec.key}.html",
                "text": f"{spec.key}.txt",
            }
        )
    (DIST / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"exported {len(manifest)} templates → {DIST}")


if __name__ == "__main__":
    main()
