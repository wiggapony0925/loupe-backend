"""Centralised cache TTLs and concurrency limits."""

from __future__ import annotations

# --- TTLs (seconds) ---
JWKS_CACHE_TTL = 3600  # Apple/Google JWKS rotate slowly
CARD_SEARCH_TTL = 300  # 5 min for upstream search results
TRENDING_TTL = 900  # 15 min for /cards/trending aggregations
CARD_DETAIL_TTL = 86400  # 24 h for individual card metadata
SET_LIST_TTL = 86400  # 24 h for set listings
PRICE_HISTORY_TTL = 600  # 10 min for price snapshots
USER_PROFILE_TTL = 60  # /me responses (small)
SCAN_JOB_TTL = 30  # status polling

# --- Sizes ---
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

# --- Concurrency / pool limits ---
UPSTREAM_HTTP_CONCURRENCY = 8  # per upstream client
S3_UPLOAD_CONCURRENCY = 4  # per scan (4 angles)
GRADING_PIPELINE_CONCURRENCY = 2  # per worker

# --- Redis key prefix ---
REDIS_KEY_PREFIX = "loupe"

# --- WebSocket channel template ---
SCAN_PUBSUB_CHANNEL = "loupe:scans:user:{user_id}"
