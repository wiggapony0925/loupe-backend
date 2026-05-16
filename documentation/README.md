# Loupe Backend — Documentation Package

Markdown source files that get concatenated into the FastAPI OpenAPI
`description` at app startup (see `documentation/render_openapi.py`).

- `openapi_overview.md` — Product/architecture overview shown at the top of the docs.
- `api_principles.md` — Conventions every endpoint follows (pagination, errors, auth).
- `data_lifecycle.md` — How a scan becomes a graded card (state machine).
- `endpoint_playbook.md` — Common client workflows + sample sequences.
- `tag_docs.py` — Programmatic per-tag descriptions (rendered after the static markdown).
- `url_docs.py` — Upstream catalog/pricing URLs referenced by the backend.
- `render_openapi.py` — Concatenates everything; exported as `build_full_description()`.

Edit the markdown files freely; the description rebuilds on every process boot.
