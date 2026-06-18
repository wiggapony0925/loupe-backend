# Apify — Facebook Marketplace "Near You" Listings

Powers the **Near You** carousel on the card-detail sheet: real Facebook
Marketplace listings for the viewed card, filtered to a radius around the
user's device location.

```
[card detail sheet]
  user taps "Enable Location"           (NearbyListingsSection)
  --> expo-location requestForegroundPermissionsAsync()
  --> coords { lat, lng }  (in-memory only, never persisted)

[GET /v1/cards/{card_id}/nearby-listings?lat=&lng=&radius_km=&limit=]
  --> nearby_listings_service.get_nearby_listings_for_card(...)
        - resolve card -> build search query ("<name> <set> #<number>")
        - cache check (Redis, key rounded to ~1km grid)
        - ApifyProvider.search_nearby_listings(query, lat, lng, radius_km, limit)
            POST https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token=
            payload: { query, latitude, longitude, radiusKm, maxItems }
        - map dataset items -> listings, sort closest-first
        - cache set (TTL 300s)
  --> { card_id, query, center, radius_km, listings: [...] }
```

## Configuration

Backend `.env` only (token never reaches the client):

```
APIFY_API_TOKEN=apify_api_...
APIFY_FB_MARKETPLACE_ACTOR=apify/facebook-marketplace-scraper
```

Blank `APIFY_API_TOKEN` disables the feature gracefully — the endpoint still
returns `200` with `listings: []`, and the client renders a clean empty state.

## Backend pieces

| File | Role |
| --- | --- |
| `app/config.py` | `apify_api_token`, `apify_fb_marketplace_actor` settings |
| `app/integrations/apify.py` | `ApifyProvider.search_nearby_listings()` — calls the actor, maps items, swallows errors → `[]` |
| `app/services/market/nearby_listings_service.py` | resolves card → query, caches per-neighbourhood, sorts closest-first |
| `app/schemas/nearby_listings.py` | response wire shape |
| `app/routers/catalog/cards.py` | `GET /{card_id}/nearby-listings` endpoint |
| `tests/market/test_nearby_listings.py` | endpoint shape, 422/404, item mapping |

`ApifyProvider` is **not** part of the capability fan-out registry: the
nearby search needs geo arguments, so the service calls it directly. It still
reuses `BaseProvider`'s shared HTTP client, retry helper, and
error-swallowing conventions.

## Frontend pieces

| File | Role |
| --- | --- |
| `src/application/location/useUserLocation.ts` | foreground permission + coords; never auto-prompts |
| `src/application/queries/catalog/useCardNearbyListings.ts` | React Query hook; disabled until coords exist |
| `src/presentation/features/cardDetail/NearbyListingsSection.tsx` | 4 states: enable-CTA / loading / results / empty |
| `src/presentation/features/cardDetail/NearbyListingTile.tsx` | tile with distance / location chip |
| `app/card/[id].tsx` | renders `<NearbyListingsSection>` below `<LiveListingsSection>` |

App config for permissions lives in `app.json`: the `expo-location` plugin
(`locationWhenInUsePermission`) plus Android `ACCESS_COARSE/FINE_LOCATION`.

## Privacy

- Permission is requested **only** when the user taps *Enable Location*.
- Raw `lat`/`lng` live in component state, are sent to our backend over HTTPS,
  and are forwarded to Apify. They are never logged or persisted.
- The Redis cache key rounds coordinates to 2 dp (~1.1 km) so we cache per
  neighbourhood, not per GPS jitter, and raw coordinates never become part of
  a long-lived key.

## Apify actor output mapping

Actors vary in their dataset shape, so `_map_item` reads defensively across
common key spellings and drops any item missing a usable price + title:

| Field | Keys tried |
| --- | --- |
| title | `title`, `name`, `marketplace_listing_title` |
| price | `price`, `amount`, `listing_price`, `priceAmount` |
| url | `url`, `listingUrl`, `link`, `permalink` |
| image | `image`, `imageUrl`, `primaryImage`, `thumbnail`, `primary_listing_photo` |
| condition | `condition`, `itemCondition` |
| currency | `currency`, `priceCurrency` (default `USD`) |
| distance | `distanceKm`, `distance_km`, `distance` |
| location | `location`, `locationLabel`, `city`, `locationText` |

If you swap actors, adjust the payload keys in
`ApifyProvider.search_nearby_listings` and the read keys in `_map_item`.
