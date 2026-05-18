"""``/test-users`` doc page — renders the canonical persona registry.

Imported by :mod:`documentation.docs_auth` so the same access-gate
applies. Two endpoints:

- ``GET /test-users``        → styled HTML table, one row per persona.
- ``GET /test-users.json``   → machine-readable list (useful for the QA
  team's automated login walks).
"""

from __future__ import annotations

from html import escape

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from documentation.docs_auth import _denied_response, _verify_docs_access
from documentation.test_personas import BANDS, DEFAULT_PASSWORD, PERSONAS, Persona


# ─────────────────────────────────────────────────────────────────────────
# Serialisation
# ─────────────────────────────────────────────────────────────────────────
def _persona_to_dict(p: Persona) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "email": p.email,
        "password": p.password,
        "archetype": p.archetype,
        "vault_size": p.vault_size,
        "avg_grade": p.avg_grade,
        "scanner_profile": p.scanner_profile,
        "auth": p.auth,
        "tenure_days": p.tenure_days,
        "headline": p.headline,
        "why_unique": p.why_unique,
        "tags": list(p.tags),
    }


# ─────────────────────────────────────────────────────────────────────────
# HTML rendering
# ─────────────────────────────────────────────────────────────────────────
_PAGE_CSS = """
:root {
  --bg: #0B0B0D; --panel: #131316; --row: #1C1C1E; --row-alt: #17171A;
  --ink: #F5F5F7; --muted: #8E8E93; --accent: #00F59B; --border: #2C2C2E;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.55; }
.container { max-width: 1280px; margin: 0 auto; padding: 48px 32px 96px; }
.hero h1 { font-size: 36px; margin: 0 0 8px; letter-spacing: -0.02em; }
.hero p  { color: var(--muted); margin: 0 0 8px; max-width: 720px; }
.hero a  { color: var(--accent); text-decoration: none; }
.cred { display: inline-flex; gap: 12px; align-items: center;
  background: var(--panel); border: 1px solid var(--border);
  padding: 10px 16px; border-radius: 10px; margin-top: 20px; font-size: 14px; }
.cred code { color: var(--accent); font-family: "SF Mono", Menlo, monospace; }
.band { margin-top: 56px; }
.band-head { display: flex; align-items: baseline; gap: 16px;
  border-bottom: 1px solid var(--border); padding-bottom: 12px; margin-bottom: 0; }
.band-head h2 { margin: 0; font-size: 22px; }
.band-head .count { color: var(--muted); font-size: 13px; }
.band-head .desc { color: var(--muted); font-size: 14px; flex: 1; text-align: right; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th { text-align: left; font-weight: 500; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em; font-size: 11px;
  padding: 14px 10px; border-bottom: 1px solid var(--border); position: sticky; top: 0;
  background: var(--bg); }
tbody td { padding: 14px 10px; vertical-align: top; border-bottom: 1px solid var(--border); }
tbody tr:nth-child(odd) td { background: var(--row-alt); }
tbody tr:nth-child(even) td { background: var(--row); }
.id { color: var(--muted); font-variant-numeric: tabular-nums; }
.email { font-family: "SF Mono", Menlo, monospace; color: var(--accent); white-space: nowrap; }
.num { font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 999px;
  background: rgba(0, 245, 155, 0.12); color: var(--accent);
  font-size: 11px; font-weight: 500; letter-spacing: 0.02em; }
.pill.alt { background: rgba(142, 142, 147, 0.18); color: var(--ink); }
.pill.warn { background: rgba(255, 159, 10, 0.18); color: #FF9F0A; }
.pill.danger { background: rgba(255, 69, 58, 0.18); color: #FF453A; }
.tags { display: flex; flex-wrap: wrap; gap: 4px; }
.tags span { background: rgba(255,255,255,0.06); color: var(--muted);
  font-size: 11px; padding: 2px 6px; border-radius: 4px; }
.why { color: var(--muted); font-size: 12px; max-width: 280px; }
.headline { font-weight: 500; }
.back { display: inline-flex; align-items: center; gap: 6px; color: var(--accent);
  text-decoration: none; font-size: 14px; margin-bottom: 24px; }
.back:hover { opacity: 0.8; }
"""

_AUTH_PILL = {
    "password": ("pill", "Password"),
    "apple": ("pill alt", "Apple SSO"),
    "google": ("pill alt", "Google SSO"),
}

_SCANNER_PILL = {
    "none": ("pill alt", "—"),
    "fresh": ("pill", "1 · just paired"),
    "active": ("pill", "1 · BLE active"),
    "offline": ("pill warn", "1 · offline 30d"),
    "dual": ("pill", "2 · BLE+WiFi"),
    "multi": ("pill", "3 · all transports"),
    "fleet": ("pill", "5 · fleet"),
}


def _pill(spec: tuple[str, str]) -> str:
    cls, label = spec
    return f'<span class="{cls}">{escape(label)}</span>'


def _row(p: Persona) -> str:
    auth = _pill(_AUTH_PILL.get(p.auth, ("pill alt", p.auth)))
    scanner = _pill(_SCANNER_PILL.get(p.scanner_profile, ("pill alt", p.scanner_profile)))
    avg = f"{p.avg_grade:.1f}" if p.vault_size > 0 else "—"
    vault = f"{p.vault_size:,}" if p.vault_size > 0 else "—"
    tenure = "today" if p.tenure_days == 0 else (
        f"{p.tenure_days}d" if p.tenure_days < 365 else f"{p.tenure_days / 365:.1f}y"
    )
    tags = "".join(f"<span>{escape(t)}</span>" for t in p.tags)
    pw_cell = (
        f'<code>{escape(DEFAULT_PASSWORD)}</code>' if p.auth == "password" else "—"
    )
    return (
        "<tr>"
        f'<td class="id">#{p.id:02d}</td>'
        f"<td class=\"headline\">{escape(p.name)}<div class=\"why\">{escape(p.headline)}</div></td>"
        f'<td class="email">{escape(p.email)}</td>'
        f"<td>{pw_cell}</td>"
        f'<td class="num">{vault}</td>'
        f'<td class="num">{avg}</td>'
        f"<td>{scanner}</td>"
        f"<td>{auth}</td>"
        f'<td class="num">{escape(tenure)}</td>'
        f'<td class="why">{escape(p.why_unique)}</td>'
        f'<td><div class="tags">{tags}</div></td>'
        "</tr>"
    )


def _band(name: str, desc: str, ids: range) -> str:
    members = [p for p in PERSONAS if p.id in ids]
    body = "\n".join(_row(p) for p in members)
    return (
        '<section class="band">'
        '<div class="band-head">'
        f"<h2>{escape(name)}</h2>"
        f'<span class="count">{len(members)} personas</span>'
        f'<span class="desc">{escape(desc)}</span>'
        "</div>"
        "<table>"
        "<thead><tr>"
        "<th>#</th><th>Name</th><th>Email</th><th>Password</th>"
        "<th class=\"num\">Vault</th><th class=\"num\">Avg</th>"
        "<th>Scanners</th><th>Auth</th>"
        "<th class=\"num\">Tenure</th><th>Why unique</th><th>Tags</th>"
        "</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table>"
        "</section>"
    )


def _render_page() -> str:
    bands_html = "\n".join(_band(name, desc, ids) for name, desc, ids in BANDS)
    total = len(PERSONAS)
    total_vault = sum(p.vault_size for p in PERSONAS)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Loupe · Test users</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>{_PAGE_CSS}</style>
</head>
<body>
  <div class="container">
    <a class="back" href="/api-docs">← Back to API docs</a>
    <header class="hero">
      <h1>Test users</h1>
      <p>{total} seeded demo accounts covering every meaningful state of
      the product — empty vaults, single-card collectors, multi-device
      whales, SSO flavors, and edge cases. Each persona's "why unique"
      column explains what makes it different from the others.</p>
      <p>Seed via <code>python -m scripts.seed_test_users</code>. Raw
      data: <a href="/test-users.json">/test-users.json</a>.</p>
      <div class="cred">
        Password for all <code>password</code> personas:
        <code>{escape(DEFAULT_PASSWORD)}</code>
        <span style="color: var(--muted)">·</span>
        <span style="color: var(--muted)">{total_vault:,} graded cards total</span>
      </div>
    </header>
    {bands_html}
  </div>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────
# Route registration
# ─────────────────────────────────────────────────────────────────────────
def register_test_users_route(app: FastAPI) -> None:
    """Attach ``/test-users`` and ``/test-users.json`` to *app*."""

    @app.get("/test-users", include_in_schema=False)
    async def test_users_page(request: Request) -> HTMLResponse:
        if not _verify_docs_access(request):
            return _denied_response()
        return HTMLResponse(_render_page())

    @app.get("/test-users.json", include_in_schema=False)
    async def test_users_json(request: Request) -> JSONResponse:
        if not _verify_docs_access(request):
            return JSONResponse({"detail": "Access token required"}, status_code=403)
        return JSONResponse(
            {
                "default_password": DEFAULT_PASSWORD,
                "count": len(PERSONAS),
                "personas": [_persona_to_dict(p) for p in PERSONAS],
            }
        )
