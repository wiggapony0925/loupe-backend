# Endpoints the contracts depend on but cannot verify

Regenerate with:

```bash
python scripts/verify_consumer_contracts.py --list-unverifiable
```

## What this list is

These 47 operations have **no `response_model`**, so FastAPI emits
`{"type": "object", "additionalProperties": true}` for them. That schema permits
any shape, which means a claim like "this response has a `summary.raw.amount`"
can be neither confirmed nor denied. The verifier reports them as UNVERIFIABLE
instead of passing them, because a green check that quietly skipped them would
be a false assurance.

**306 of the 1,635 contracted fields — 19% — land here.** The other 1,329 are
genuinely checked against a declared schema.

## Why it is worth closing

Adding a `response_model` to one of these converts every field the clients read
from it into a checked assertion, at no cost to the client. The top of this list
is where the leverage is: `/v1/analytics/overview` alone accounts for 50 of the
306, and it is a screen where a silently renamed field shows up as a blank
dashboard rather than an error.

It also buys correct OpenAPI for the generated TypeScript. Right now
`npm run generate:api-types` types these as `Record<string, unknown>`, so the
clients hand-write wire types for them — which is exactly the duplication the
generated types exist to remove.

## The list, by how much it would buy

Count is the number of contracted fields that become verifiable.

| Fields | Operation |
| ---: | --- |
| 50 | `GET /v1/analytics/overview` |
| 25 | `GET /v1/cards/{card_id}/market` |
| 19 | `GET /v1/admin/pricecharting` |
| 17 | `GET /v1/home/feed` |
| 12 | `GET /v1/public/carousels/rail` |
| 11 | `GET /v1/public/carousels/resolved` |
| 11 | `GET /v1/cards/{card_id}/listings` |
| 10 | `GET /v1/cards/{card_id}/nearby-listings` |
| 9 | `GET /v1/sets/{set_id}/checklist` |
| 9 | `GET /v1/cards/{card_id}/comps` |
| 9 | `GET /v1/admin/card-tree` |
| 8 | `GET /v1/sets/progress` |

The remaining 35 operations carry 1–7 fields each. Run the command above for the
full list — it is generated from the live schema, so it shrinks on its own as
response models land.

## How to close one

Declare what the endpoint already returns:

```python
@router.get("/analytics/overview", response_model=AnalyticsOverviewRead)
async def analytics_overview(...) -> AnalyticsOverviewRead:
    ...
```

Then re-run the verifier. Fields that were `?` become checked, and any place
where the model and the real payload disagree surfaces immediately — which is
the point, and occasionally the surprise.
