# Loupe — Productionization Roadmap

> A living checklist of everything between "tests are green on my laptop"
> and "real users buying real cards". Update freely.

---

## 🔑 API Keys & External Services

| Provider | Purpose | Cost | Status | Setup URL | Env var name |
|---|---|---|---|---|---|
| Pokémon TCG API | Card catalog enrichment | Free | ⚠️ Optional (works keyless at 30 req/hr) | https://dev.pokemontcg.io/ | `POKEMON_TCG_API_KEY` |
| Scryfall | MTG card catalog | Free, no key | ✅ Live | https://scryfall.com/docs/api | (none) |
| YGOPRODeck | Yu-Gi-Oh! catalog | Free, no key | ✅ Live | https://ygoprodeck.com/api-guide/ | (none) |
| eBay Browse API | Live sold-comp prices (universal) | Free dev tier | 🔲 Need to register | https://developer.ebay.com → My Account → Application Keys → "Production" | `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` |
| TCGplayer Pricing API | Authoritative TCG market prices | Partner-only | 🔲 Apply (weeks for approval) | https://docs.tcgplayer.com/ → "Get Approved" | `TCGPLAYER_PUBLIC_KEY`, `TCGPLAYER_PRIVATE_KEY` |
| PriceCharting | Graded sports / Pokémon sales history | ~$10/mo | 🔲 Pay if needed | https://www.pricecharting.com/api-documentation | `PRICECHARTING_API_TOKEN` |
| Sports Card Investor / 130point | Sports-card sold comps | No public API | 🔲 Defer (would require scraping) | n/a | n/a |
| Apple Sign-In | User auth | Free (requires Apple Developer Program $99/yr) | 🔲 Need to register Service ID | https://developer.apple.com/account/resources/identifiers → Services IDs | `APPLE_CLIENT_ID`, `APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY` |
| Google OAuth | User auth | Free | 🔲 Need to create iOS + Web OAuth client | https://console.cloud.google.com → APIs & Services → Credentials | `GOOGLE_IOS_CLIENT_ID`, `GOOGLE_WEB_CLIENT_ID` |

---

## ☁️ AWS Services Required for Production

| Service | Purpose | Tier suggestion | Monthly cost est. | Status |
|---|---|---|---|---|
| ECS Fargate | Run API + arq worker containers | 2× 0.5 vCPU / 1 GB tasks | ~$30/mo | 🔲 |
| RDS Postgres | Primary database | db.t4g.micro (free-tier eligible) → db.t4g.small for prod | $0 → $25/mo | 🔲 |
| ElastiCache Redis | arq queue + cache + pub/sub | cache.t4g.micro | ~$12/mo | 🔲 |
| S3 | Scanner image uploads + thumbnails | Standard, lifecycle rule to Glacier after 30d | ~$5/mo for first 100 GB | 🔲 |
| CloudFront | CDN for card images + thumbnails | PriceClass_100 | ~$1/mo + bandwidth | 🔲 |
| Route 53 | DNS for `api.loupe.app` | $0.50/mo per hosted zone | 🔲 |
| ACM | Free TLS cert for HTTPS | Free | 🔲 |
| Secrets Manager | Store JWT private key + OAuth secrets + DB password | $0.40/mo per secret | 🔲 |
| CloudWatch | Logs + metrics | Free tier covers small scale | $0 → $10/mo | 🔲 |
| IAM | Roles for ECS task → S3 + Secrets Manager | Free | 🔲 |
| Application Load Balancer | Public HTTPS endpoint in front of ECS | ~$18/mo | 🔲 |
| (Optional) SES | Transactional email if you add password reset later | pay-per-send | 🔲 |

**Total monthly cost (small scale, ~100 active users):** roughly **$90–$120/mo** depending on traffic.

---

## 🚀 Deployment Roadmap (in order)

- [ ] Buy domain `loupe.app` (or chosen name) via Route 53 or external registrar
- [ ] Create AWS account / sub-account with billing alerts
- [ ] Provision RDS Postgres + ElastiCache Redis via Terraform or Console
- [ ] Push backend image to ECR
- [ ] Deploy ECS service (api + worker) behind ALB with ACM cert
- [ ] Run `alembic upgrade head` via one-off ECS task
- [ ] Configure Secrets Manager entries; ECS task role pulls them at boot
- [ ] Point frontend `EXPO_PUBLIC_API_URL` at `https://api.loupe.app`
- [ ] Submit Apple/Google OAuth verification for production client IDs
- [ ] Submit iOS app to TestFlight

---

## 🛠 Local Dev Quickstart

1. `cd loupe-backend && python3.12 -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt -r requirements-dev.txt`
3. `cp .env.example .env` and fill in (most values can stay blank for local dev)
4. `docker compose up -d postgres redis minio` (or skip — falls back to SQLite + in-memory cache)
5. `alembic upgrade head` (only if using Postgres)
6. `make run` → API on http://localhost:8000, docs at `/docs`
7. `make worker` (separate terminal) → arq worker
8. From frontend: `EXPO_PUBLIC_API_URL=http://localhost:8000 npm start` (iOS sim hits localhost directly; real device needs your LAN IP)

---

## 🟡 Known Gaps / TODOs

- [ ] Real ML grading pipeline (currently deterministic mock in `app/services/grading_service.py`)
- [ ] eBay sold-listings price ingestion worker (`clients/ebay.py` stubbed)
- [ ] Image thumbnailing & CDN signing
- [ ] Push notifications (APNs/FCM) for scan completion
- [ ] Rate limiting on public catalog endpoints (slowapi or AWS WAF)
- [ ] OpenAPI client codegen → frontend types (`openapi-typescript`)
- [ ] Replace ephemeral JWT keypair with stable Secrets Manager-backed RSA key
- [ ] Sports-card data source (currently no clean API; eBay + manual catalog is the path)
