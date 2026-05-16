# Loupe API Contract — `v1`

> **Status:** Frozen for `v1`.
> **Last touched:** see git log on this file.
> **Owners:** loupe-backend + loupe-frontend.

This document is the single source of truth for the public API surface of
`loupe-backend` and the contract that `loupe-frontend` (React Native /
Expo) consumes. Both repos MUST stay in lock-step with this file:

* Server response shapes are enforced by `app/response_envelope.py` (HTTP)
  and `app/ws_manager.py` (WebSocket).
* TypeScript twin types live in `loupe-frontend/src/api/types.ts`.

If you intend to change a shape, update this file in the same PR.

---

## Table of contents

1. [Overview](#1-overview)
2. [Universal envelope](#2-universal-envelope)
3. [`meta` block](#3-meta-block)
4. [`pagination` block](#4-pagination-block)
5. [`error` block + error codes](#5-error-block--error-codes)
6. [Common atoms](#6-common-atoms)
7. [Entities (17 frozen shapes)](#7-entities-17-frozen-shapes)
8. [Endpoint index](#8-endpoint-index)
9. [WebSocket protocol](#9-websocket-protocol)
10. [Versioning](#10-versioning)
11. [Idempotency](#11-idempotency)
12. [Rate limits & caching](#12-rate-limits--caching)
13. [Frontend integration notes](#13-frontend-integration-notes)

---

## 1. Overview

The Loupe API is JSON-over-HTTPS with a single mounted prefix:

```
https://api.loupe.app/v1/...
```

* **Encoding:** UTF-8 JSON, `snake_case` keys on every field.
* **Auth:** `Authorization: Bearer <access_token>` (JWT, see
  `/v1/auth/...`).
* **System endpoints** (`/health`, `/version`, `/metrics`, `/docs`,
  `/redoc`, `/openapi.json`, `/ws/*`) live at the **root** and are
  **exempt** from the envelope wrapper — they keep their narrow,
  load-balancer-friendly shapes.
* **Everything else** (the whole `/v1/...` surface) is wrapped in the
  universal envelope defined in §2.

### Compromises made for this freeze

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | `snake_case` JSON on both wire and TS types. | Backend is Python-first; round-tripping camel↔snake doubles type maintenance and breaks raw `curl` debugging. The frontend simply types `card.image_url` instead of `card.imageUrl`. |
| 2 | Auto-wrap response middleware instead of rewriting every router. | Allows the entire 17-entity surface to ship the envelope **today** with zero per-route churn. Per-route schema enrichment (richer shapes than the current models expose) is tracked separately as a follow-up. |
| 3 | Missing model columns (e.g. scanner `battery_pct`, `signal_strength`) emit `null` rather than blocking on migrations. | Lets the frontend code against the frozen shape immediately; nullable fields are typed as `T | null` end-to-end. |
| 4 | `request_id` is 24-char hex (`secrets.token_hex(12)`) — not a full ULID. | Lightweight, no extra dependency, k-sortable enough for log correlation. Clients may pass `X-Request-Id` to override. |
| 5 | WS frames use a parallel-but-distinct envelope (`{type, ts, request_id, data}`) instead of the HTTP one. | Pub/sub channels are typed; a `meta`+`pagination`+`error` block would be dead weight on every frame. |

---

## 2. Universal envelope

Every `/v1/*` response — success **and** error — has the same top-level
shape:

```jsonc
{
  "data": <T> | null,            // present on success
  "meta": { ... },               // ALWAYS present
  "pagination": { ... } | null,  // present on paginated endpoints
  "error": { ... } | null        // present on failure
}
```

### Success example

```json
{
  "data": { "id": "9c3f...", "email": "alice@example.com", "display_name": "Alice" },
  "meta": {
    "request_id": "a1b2c3d4e5f60718293a4b5c",
    "timestamp": "2025-01-31T18:42:01.234Z",
    "version": "v1",
    "duration_ms": 12.7
  },
  "pagination": null,
  "error": null
}
```

### Paginated success example

```json
{
  "data": [
    { "id": "card_1", "name": "Charizard", "tcg": "pokemon" },
    { "id": "card_2", "name": "Pikachu",   "tcg": "pokemon" }
  ],
  "meta": {
    "request_id": "11aa22bb33cc44dd55ee66ff",
    "timestamp": "2025-01-31T18:42:01.345Z",
    "version": "v1",
    "duration_ms": 28.4
  },
  "pagination": {
    "page": 1,
    "page_size": 25,
    "total": 142,
    "total_pages": 6,
    "has_next": true,
    "has_prev": false,
    "next_cursor": null,
    "prev_cursor": null
  },
  "error": null
}
```

### Error example

```json
{
  "data": null,
  "meta": {
    "request_id": "ffeeddccbbaa99887766",
    "timestamp": "2025-01-31T18:42:01.456Z",
    "version": "v1",
    "duration_ms": 4.1
  },
  "pagination": null,
  "error": {
    "code": "auth.invalid_credentials",
    "message": "Invalid refresh token.",
    "status": 401,
    "field": null,
    "details": null
  }
}
```

### Invariants

* `meta` is **always** present.
* On success: `data` is non-null (may be an empty list / object), `error`
  is `null`.
* On failure: `data` is `null`, `error` is non-null.
* `pagination` is non-null **only** on paginated list endpoints.
* HTTP status code mirrors `error.status` on failure; on success it is
  the natural router status (`200`, `201`, `204`, …). `204 No Content`
  is the **only** envelope-exempt success status.

---

## 3. `meta` block

| Field         | Type     | Notes |
|---------------|----------|-------|
| `request_id`  | string   | 24-char hex (or whatever the client sent via `X-Request-Id`). Mirrored in the `X-Request-Id` response header. |
| `timestamp`   | string   | ISO-8601 UTC, millisecond precision, trailing `Z`. |
| `version`     | string   | API contract version. Currently `"v1"`. |
| `duration_ms` | number\|null | Server-side wall time, milliseconds, fractional. `null` if the request log middleware did not record a start time (should not happen in practice). |

The frontend SHOULD echo `request_id` into bug reports / Sentry breadcrumbs.

---

## 4. `pagination` block

```ts
type Pagination = {
  page: number;          // 1-based
  page_size: number;     // requested size, clamped server-side
  total: number;         // total matching rows
  total_pages: number;   // ceil(total / page_size)
  has_next: boolean;
  has_prev: boolean;
  next_cursor: string | null;  // reserved for cursor-based endpoints
  prev_cursor: string | null;  // reserved for cursor-based endpoints
};
```

Cursor fields are reserved for endpoints that move off pure offset
pagination (price history streams, audit logs). Today they are always
`null`.

---

## 5. `error` block + error codes

```ts
type ErrorDetail = {
  code: string;             // dotted machine code, see table
  message: string;          // human-readable, EN, no PII
  status: number;           // HTTP status mirror
  field: string | null;     // for validation errors — dotted field path
  details: unknown | null;  // optional structured payload
};
```

### Default codes by HTTP status

| HTTP | Default `error.code`         | Meaning |
|------|------------------------------|---------|
| 400  | `request.bad`                | Malformed request. |
| 401  | `auth.unauthorized`          | Missing / invalid bearer. |
| 403  | `auth.forbidden`             | Authenticated but lacks permission. |
| 404  | `resource.not_found`         | Target object does not exist (or not visible). |
| 405  | `request.method_not_allowed` | Wrong HTTP verb. |
| 409  | `resource.conflict`          | State / uniqueness conflict. |
| 410  | `resource.gone`              | Resource permanently removed. |
| 415  | `request.unsupported_media`  | Wrong `Content-Type`. |
| 422  | `validation.failed`          | Pydantic validation error (`field` set). |
| 429  | `rate.limited`               | Throttled — retry after `Retry-After`. |
| 500  | `internal.unexpected`        | Server bug — logged with `request_id`. |
| 502  | `upstream.bad_gateway`       | Card-catalog upstream failed. |
| 503  | `service.unavailable`        | Dependency (Redis/DB/S3) down. |
| 504  | `upstream.timeout`           | Upstream took too long. |

Routers may raise `HTTPException(status_code=..., detail={"code": "...",
"message": "...", "field": "...", "details": {...}})` to override the
default code.

---

## 6. Common atoms

| Type | Shape | Notes |
|------|-------|-------|
| `ID` | `string` | Always a UUID v4 stringified, except for composite catalog IDs (`"pokemontcg:base1-4"`). |
| `ISODate` | `string` | `"YYYY-MM-DDTHH:MM:SS.sssZ"`, UTC. |
| `Currency` | `string` | ISO-4217, uppercase (`"USD"`, `"EUR"`). |
| `Money` | `{ amount: number; currency: Currency }` | Decimals as JSON numbers — clients should treat as `string | number` and round to 2dp for display. |
| `ImageAsset` | `{ url: string; width: number \| null; height: number \| null }` | |
| `ImageSet` | `{ small: ImageAsset \| null; normal: ImageAsset \| null; large: ImageAsset \| null }` | |
| `Tcg` | `"pokemon" \| "magic" \| "yugioh"` | |
| `ScanAngle` | `"front" \| "back" \| "top" \| "bottom" \| "left" \| "right"` | |

---

## 7. Entities (17 frozen shapes)

All shapes use `snake_case` keys. Fields explicitly listed as nullable
may emit `null`; fields not in the model today emit `null` (compromise
#3).

### 1. `User`

```ts
type User = {
  id: ID;
  email: string;
  display_name: string | null;
  avatar_url: string | null;
  created_at: ISODate;
  updated_at: ISODate;
  deleted_at: ISODate | null;
};
```

### 2. `UserSettings`

```ts
type UserSettings = {
  user_id: ID;
  currency: Currency;          // default "USD"
  theme: "system" | "light" | "dark";
  notifications_enabled: boolean;
  default_grader: "psa" | "bgs" | "cgc" | null;
  updated_at: ISODate;
};
```

### 3. `Scanner`

```ts
type Scanner = {
  id: ID;
  user_id: ID;
  device_id: string;            // hardware-stable identifier
  name: string;
  firmware_version: string | null;
  battery_pct: number | null;   // emitted as null until model column lands
  signal_strength: number | null;
  last_seen_at: ISODate | null;
  paired_at: ISODate;
  created_at: ISODate;
};
```

### 4. `CardSet`

```ts
type CardSet = {
  id: ID;                       // composite ("pokemontcg:base1") or UUID
  tcg: Tcg;
  code: string;                 // upstream set code
  name: string;
  release_date: ISODate | null;
  card_count: number | null;
  logo_url: string | null;
  source: string;               // "pokemontcg" | "scryfall" | "ygoprodeck" | "loupe-db"
};
```

### 5. `Card`

```ts
type Card = {
  id: ID;                       // composite or UUID
  tcg: Tcg;
  name: string;
  number: string | null;
  rarity: string | null;
  set_id: ID | null;
  set_name: string | null;
  set_code: string | null;
  image_url: string | null;     // back-compat top-level
  images: ImageSet | null;
  year: number | null;
  source: string;
};
```

### 6. `PricingSummary`

```ts
type PricingSummary = {
  card_id: ID;
  currency: Currency;
  low: number | null;
  mid: number | null;
  high: number | null;
  market: number | null;
  updated_at: ISODate | null;
  source: string | null;
};
```

### 7. `PricePoint`

```ts
type PricePoint = {
  ts: ISODate;
  price: number;
  currency: Currency;
  source: string;
};
```

### 8. `PriceHistory`

```ts
type PriceHistory = {
  card_id: ID;
  currency: Currency;
  points: PricePoint[];
  granularity: "daily" | "weekly" | "monthly";
};
```

### 9. `GradedCard`

```ts
type GradedCard = {
  id: ID;
  user_id: ID;
  card_id: ID;
  grader: "psa" | "bgs" | "cgc";
  grade: number;                // 1-10 (PSA/CGC) or 1-10 with 0.5 steps (BGS)
  cert_number: string | null;
  subgrades: Subgrades | null;
  notes: string | null;
  scanned_at: ISODate | null;
  created_at: ISODate;
  updated_at: ISODate;
};
```

### 10. `Subgrades`

```ts
type Subgrades = {
  centering: SubgradeDetail | null;
  corners:   SubgradeDetail | null;
  edges:     SubgradeDetail | null;
  surface:   SubgradeDetail | null;
};
```

### 11. `SubgradeDetail`

```ts
type SubgradeDetail = {
  score: number;              // 1-10
  confidence: number | null;  // 0..1
  notes: string | null;
};
```

### 12. `FingerprintSummary`

```ts
type FingerprintSummary = {
  card_id: ID;
  hash: string;                 // pHash / dHash hex
  algorithm: "phash" | "dhash" | "ahash";
  similarity: number | null;    // 0..1 for nearest neighbour matches
  matched_card_id: ID | null;
};
```

### 13. `ScanJob`

```ts
type ScanJob = {
  id: ID;
  user_id: ID;
  scanner_id: ID | null;
  status:
    | "queued"
    | "uploading"
    | "processing"
    | "complete"
    | "failed";
  angles: ScanAngle[];
  uploaded_angles: ScanAngle[];
  progress: number;             // 0..1
  graded_card_id: ID | null;    // populated when status="complete"
  created_at: ISODate;
  updated_at: ISODate;
  completed_at: ISODate | null;
};
```

### 14. `Collection`

```ts
type Collection = {
  id: ID;
  user_id: ID;
  name: string;
  description: string | null;
  cover_image_url: string | null;
  item_count: number;
  created_at: ISODate;
  updated_at: ISODate;
};
```

### 15. `CollectionItem`

```ts
type CollectionItem = {
  id: ID;
  collection_id: ID;
  graded_card_id: ID;
  note: string | null;
  added_at: ISODate;
};
```

### 16. `ApiKey`

```ts
type ApiKey = {
  id: ID;
  user_id: ID;
  label: string;
  prefix: string;                // first 8 chars, safe to display
  last_used_at: ISODate | null;
  expires_at: ISODate | null;
  created_at: ISODate;
  revoked_at: ISODate | null;
};
```

### 17. `AuditLogEntry`

```ts
type AuditLogEntry = {
  id: ID;
  user_id: ID | null;
  action: string;                // dotted, e.g. "scanner.paired"
  target_type: string | null;
  target_id: ID | null;
  ip_address: string | null;
  user_agent: string | null;
  occurred_at: ISODate;
  metadata: Record<string, unknown> | null;
};
```

### Bonus auth atom — `TokenPair`

```ts
type TokenPair = {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;            // seconds
  user: User;
};
```

---

## 8. Endpoint index

> Every entry below returns the envelope from §2. Shown column is the
> shape of `data` (and `pagination`, where applicable).

### Auth (`/v1/auth`)

| Verb | Path                  | `data` shape | Notes |
|------|-----------------------|--------------|-------|
| POST | `/auth/apple`         | `TokenPair`  | Sign-in via Apple. |
| POST | `/auth/google`        | `TokenPair`  | Sign-in via Google. |
| POST | `/auth/refresh`       | `TokenPair`  | Rotate access + refresh. |

### Users (`/v1/me`)

| Verb  | Path             | `data` shape    |
|-------|------------------|-----------------|
| GET   | `/me`            | `User`          |
| PATCH | `/me`            | `User`          |
| GET   | `/me/settings`   | `UserSettings`  |
| PATCH | `/me/settings`   | `UserSettings`  |

### Scanners (`/v1/scanners`)

| Verb   | Path                              | `data` shape  |
|--------|-----------------------------------|---------------|
| GET    | `/scanners`                       | `Scanner[]`   |
| POST   | `/scanners`                       | `Scanner` (201) |
| GET    | `/scanners/{id}`                  | `Scanner`     |
| PATCH  | `/scanners/{id}`                  | `Scanner`     |
| DELETE | `/scanners/{id}`                  | `null` (204)  |
| POST   | `/scanners/{id}/heartbeat`        | `Scanner`     |

### Cards (`/v1/cards`, `/v1/sets`)

| Verb | Path                  | `data` shape                                                            |
|------|-----------------------|-------------------------------------------------------------------------|
| GET  | `/cards`              | `Card[]` + `pagination` (DB-backed legacy paginated search).            |
| GET  | `/cards/search`       | `{ results: Card[]; total: number; source: string; error?: string }`    |
| GET  | `/cards/{id}`         | `Card`                                                                  |
| GET  | `/sets`               | `{ results: CardSet[]; total: number; source: string }`                 |

### Scans (`/v1/scans`)

| Verb | Path                   | `data` shape                                                                 |
|------|------------------------|------------------------------------------------------------------------------|
| GET  | `/scans`               | `ScanJob[]`                                                                  |
| POST | `/scans`               | `{ job: ScanJob; uploads: Array<{ angle: ScanAngle; upload_url: string; s3_key: string }> }` (201) |
| GET  | `/scans/{id}`          | `ScanJob`                                                                    |
| POST | `/scans/{id}/complete` | `ScanJob`                                                                    |

### Grades (`/v1/grades`)

| Verb   | Path                   | `data` shape    |
|--------|------------------------|-----------------|
| GET    | `/grades`              | `GradedCard[]`  |
| GET    | `/grades/{id}`         | `GradedCard`    |
| POST   | `/grades`              | `GradedCard`    |
| PATCH  | `/grades/{id}`         | `GradedCard`    |
| DELETE | `/grades/{id}`         | `null` (204)    |

### Prices (`/v1/prices`)

| Verb | Path                                | `data` shape                |
|------|-------------------------------------|-----------------------------|
| GET  | `/prices/{card_id}/summary`         | `PricingSummary`            |
| GET  | `/prices/{card_id}/history`         | `PriceHistory`              |

### Collections (`/v1/collections`)

| Verb   | Path                                  | `data` shape                              |
|--------|---------------------------------------|-------------------------------------------|
| GET    | `/collections`                        | `Collection[]`                            |
| POST   | `/collections`                        | `Collection` (201)                        |
| GET    | `/collections/{id}`                   | `Collection`                              |
| PATCH  | `/collections/{id}`                   | `Collection`                              |
| DELETE | `/collections/{id}`                   | `null` (204)                              |
| POST   | `/collections/{id}/items`             | `CollectionItem` (201)                    |
| DELETE | `/collections/{id}/items/{item_id}`   | `null` (204)                              |

### System (root, envelope-exempt)

| Verb | Path     | Body shape                                  |
|------|----------|---------------------------------------------|
| GET  | `/health`  | `{ status, uptime_seconds, redis }`       |
| GET  | `/version` | `{ name, version, env }`                  |
| GET  | `/metrics` | `{ uptime_seconds }`                      |
| GET  | `/docs`, `/redoc`, `/openapi.json` | upstream defaults |

---

## 9. WebSocket protocol

**Endpoint:** `GET /ws/scans?token=<access_jwt>` (101 Switching Protocols).

Every frame the server sends is a JSON object with the universal WS
envelope:

```ts
type WsFrame<T = unknown> = {
  type: string;        // event type, dotted
  ts: ISODate;         // server time, ms precision
  request_id: string;  // correlate to HTTP if available
  data: T;
};
```

### Frame catalogue

| `type`              | `data` shape                                                                 | When |
|---------------------|------------------------------------------------------------------------------|------|
| `hello`             | `{ user_id: ID }`                                                            | First frame after upgrade. |
| `scan.progress`     | `{ scan_id: ID; status: ScanJob["status"]; progress: number; angle?: ScanAngle }` | Worker pushes per-stage updates. |
| `scan.complete`     | `ScanJob`                                                                    | Terminal success — full job snapshot. |
| `scan.failed`       | `{ scan_id: ID; reason: string; code: string }`                              | Terminal failure. |
| `ping`              | `{}`                                                                         | Reserved server keepalive. |

Client → server frames are treated as opaque keepalives (received and
discarded). The connection is closed with `1008 Policy Violation` on
auth failure.

---

## 10. Versioning

* Path-prefixed (`/v1`). A breaking change ships under a new prefix
  (`/v2`) and the old prefix remains operational until at least 90 days
  of frontend release coverage.
* Additive changes (new optional fields, new endpoints, new error codes)
  are NOT breaking and are made under `/v1`.
* `meta.version` reflects the prefix the request hit.

---

## 11. Idempotency

* `POST` endpoints that create resources accept an optional
  `Idempotency-Key` header (UUID v4 recommended). Replays within 24h
  return the original response with the original `request_id`.
* `PATCH`/`DELETE` are inherently idempotent by primary key; clients may
  retry safely.

---

## 12. Rate limits & caching

* **Cache-Control** is applied per path prefix by
  `app/http_middleware.py`. Public catalog reads are cached at the CDN
  for 5–60 min; user-private endpoints emit `private, no-store`.
* **Rate limiting** is enforced by a token bucket per user / IP.
  Exceeded buckets respond with `429 rate.limited` and a `Retry-After`
  header (seconds).
* WebSocket frames are not rate-limited.

---

## 13. Frontend integration notes

* `loupe-frontend/src/api/client.ts` exposes:
  * `apiFetch<T>(path, init?)` → returns `T` (auto-unwraps `data`).
  * `apiFetchEnvelope<T>(path, init?)` → returns the full `Envelope<T>`
    (use when you need `meta` / `pagination`).
  * `ApiError extends Error` → carries `code`, `status`, `field`,
    `details`, `requestId`.
* `loupe-frontend/src/api/types.ts` mirrors §6 + §7. Treat it as
  generated code — keep it 1:1 with this document.
* React Query hooks (`src/hooks/api/*`) consume `apiFetch` and therefore
  see naked `data`. To read `pagination`, hooks should call
  `apiFetchEnvelope` and surface `pagination` in their return value.
* On `ApiError`, hooks should surface `error.code` to the UI for
  toast / inline rendering — never `error.message` verbatim if the code
  is `auth.*` (the UI owns auth wording).

---

_End of contract._
