# Consumer contracts

What the clients need from this API, in a form this repo's CI can check.

Each file here is published by a consumer from its own source
(`npm run contract:publish` in `loupe-frontend`) and verified on every backend
change by `scripts/verify_consumer_contracts.py`, which runs both as
`tests/contracts/test_consumer_contracts.py` and as the **can-i-deploy** gate in
`.github/workflows/ci.yml`. A breach fails CI, and the deploy workflow only runs
after CI passes — so a change that would break a shipped client cannot reach
production.

## Why there is no Pact Broker

Pact's broker exists to coordinate **many teams deploying many services
independently**: it stores contracts, tracks which consumer version was verified
against which provider version, and answers `can-i-deploy` across that matrix.

Loupe has one API and two clients, all owned by one person. Git already stores
versioned artifacts, and CI already blocks bad merges. A broker would be a
service to host, authenticate, back up and keep alive in exchange for
coordination that does not exist here — and an unmaintained broker that everyone
routes around is worse than none.

What is kept from the Pact model:

- the contract is **consumer-driven** — derived from what the client really
  calls, not from what the API happens to expose
- the contract is a **versioned artifact** — committed, diffable, stamped with
  the consumer commit that produced it
- the **provider verifies** it in its own build
- **can-i-deploy** blocks the release

What is given up: multi-version compatibility matrices. Those need more than one
deployed version of a consumer in the wild to mean anything. The moment a second
team owns a client, `publish-contract.mjs` becomes a `pact-broker publish` call
and this file becomes a footnote.

## What is checked, and what is not

Checked — structure and HTTP semantics:

- an endpoint a consumer calls still exists, with the method it uses
- a response field a consumer marked **required** is still in the schema
- a request field the consumer does not send has not become required

Not checked, on purpose — business logic, values, and database state. A contract
that asserts on values is a second, slower copy of the test suite that fails for
reasons that are not contract breaks, and people start ignoring it.

## The honest limit

**53 of 325 operations declare no `response_model`.** FastAPI emits
`{"type": "object", "additionalProperties": true}` for those, which permits any
shape — so no field-level claim about them can be verified. The verifier reports
those as `UNVERIFIABLE` instead of passing them, because a green check that
silently skipped a sixth of the surface is a false assurance.

See the punch-list:

```bash
python scripts/verify_consumer_contracts.py --list-unverifiable
```

Adding a `response_model` to an operation converts every one of its fields from
unverifiable to actually checked. That is the highest-value follow-up here.

## Ownership

Contract failures are routed by the area that owns the endpoint. Update this
table when ownership changes — it is what turns a red build into a person.

| Area | Path prefixes | Owner |
| --- | --- | --- |
| Identity & billing | `/v1/auth`, `/v1/users`, `/v1/billing` | @wiggapony0925 |
| Catalog & search | `/v1/cards`, `/v1/sets`, `/v1/catalog`, `/v1/sealed` | @wiggapony0925 |
| Market & pricing | `/v1/cards/*/market`, `/v1/cards/*/comps`, `/v1/market` | @wiggapony0925 |
| Collection & grading | `/v1/collection`, `/v1/graded`, `/v1/alerts` | @wiggapony0925 |
| Social & moderation | `/v1/social` | @wiggapony0925 |
| Scan & identify | `/v1/cards/identify`, `/v1/scan`, `/v1/forensic` | @wiggapony0925 |
| Admin | `/v1/admin` | @wiggapony0925 |

Notification is GitHub's own failed-check mail today. If this ever wants Slack,
add a step to the `can-i-deploy` job — it is the only job whose failure means
"a client is about to break", so it is the one worth paging on.

## When the gate fires

The failure names the consumer, the endpoint, the field, and the call site. Two
legitimate ways forward:

1. **The removal was a mistake** — restore the endpoint or field.
2. **The removal is intended** — then the consumer must stop needing it *first*.
   Land the client change, run `npm run contract:publish`, commit the updated
   contract here, then land the backend change. That ordering is the whole point:
   it is the same order the deploys have to happen in.

Never fix a red gate by deleting the contract entry alone. That is the one move
that turns this from a safety net into decoration.

## Adding a consumer

1. Write `contracts/<name>.contract.json` in the consumer repo — `consumer`,
   `version`, and an `endpoints` array of `{method, path, callSite,
   requiredResponseFields, requestFields}`.
2. Add a publish script that validates it against the consumer's own source and
   writes it here.
3. Run it, and commit the result. CI picks the file up automatically —
   `test_the_api_still_provides_what_this_consumer_declared` is parameterised
   over everything in this directory.
