# Endpoint Playbook

## First-launch sign-in
1. `POST /v1/auth/apple` with `id_token` from Sign-In with Apple.
2. Persist `access_token` (15m TTL) and `refresh_token` (30d TTL).
3. `GET /v1/me` to hydrate the profile screen.
4. `GET /v1/me/settings` to apply user preferences.

## Pair a hardware scanner
1. `POST /v1/scanners` with the device serial + nickname.
2. Scanner posts heartbeats to `POST /v1/scanners/{id}/heartbeat`
   every 30s while idle.

## Grade a card
1. `POST /v1/scans` (optionally pass `scanner_id`, `angles`).
2. Upload each angle to the returned `upload_url` via HTTP `PUT`.
3. `POST /v1/scans/{id}/complete` with the list of uploaded angles.
4. Subscribe to `wss://.../ws/scans?token=<access_token>` to receive
   live progress events; the final event contains `graded_card_id`.
5. `GET /v1/grades/{graded_card_id}` to render the grade report.

## Browse the catalog
- `GET /v1/sets?tcg=pokemon&page=1` — paginated set list.
- `GET /v1/cards?q=charizard&tcg=pokemon&page=1` — text search.
- `GET /v1/cards/{id}` — full card detail with metadata blob.

## Track value over time
- `GET /v1/prices?card_id=<id>&house=psa&grade=10` returns snapshots
  collected from supported pricing sources.
