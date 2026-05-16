# API Principles

## Versioning
- All resource endpoints live under `/v1/...`.
- System endpoints (`/health`, `/version`, `/metrics`) are unversioned.

## Authentication
- Mobile clients authenticate with Apple or Google Sign-In, exchanging an
  identity token at `/v1/auth/apple` or `/v1/auth/google` for a backend
  JWT pair (`access_token`, `refresh_token`).
- All `/v1/*` endpoints (except `auth.*`) require
  `Authorization: Bearer <access_token>` headers.
- Tokens are RS256-signed; ephemeral keys are generated at boot if no
  PEM is configured.  Production deployments should set
  `JWT_PRIVATE_KEY_PEM` + `JWT_PUBLIC_KEY_PEM`.

## Pagination
List endpoints return `{ items, total, page, page_size }` envelopes.
Default `page_size` is 25; the maximum varies per endpoint.

## Errors
- `400` — Pydantic validation failure (body returned verbatim).
- `401` — missing/invalid bearer token.
- `403` — token valid but user lacks access to the resource.
- `404` — resource missing or not owned by the caller.
- `409` — conflict on idempotent operations.
- `5xx` — unhandled server errors; correlated with `X-Request-Id`.

## Caching
The middleware applies `Cache-Control` per path prefix.  Catalog data
(`/v1/sets`, `/v1/cards`) is cacheable for an hour; everything user-
scoped is `private, no-store`.

## Rate limiting
Enforced upstream of FastAPI (Cloudflare/NGINX).  The backend trusts
`X-Request-Id` if supplied so logs and traces share a correlation key.
