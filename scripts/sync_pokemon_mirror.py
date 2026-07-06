"""One-shot Pokémon catalog-mirror sync (bulk-data dump → Postgres).

Populates ``catalog_mirror_sets`` / ``catalog_mirror_cards`` with the complete
Pokémon catalog, then optionally hydrates embedded prices for the newest sets
from the live API. Idempotent and resumable — re-running only fetches sets
whose card counts changed.

Local dev (docker-compose Postgres):

    .venv/bin/python scripts/sync_pokemon_mirror.py

Against production (Cloud SQL Auth Proxy, DEPLOY.md §1):

    cloud-sql-proxy loupe-app-56235:us-central1:loupe-pg --port 5432 &
    DATABASE_URL='postgresql+asyncpg://loupe:PASS@localhost:5432/loupe' \
        .venv/bin/python scripts/sync_pokemon_mirror.py

Flags:
    --force            re-sync every set even if counts match
    --max-sets N       stop after N sets (newest first)
    --prices N         after identity sync, refresh prices for N stalest sets
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-sets", type=int, default=None)
    parser.add_argument("--prices", type=int, default=0)
    args = parser.parse_args()

    from app.services.catalog import pokemon_mirror_service as mirror

    t0 = time.monotonic()
    stats = await mirror.sync_pokemon_from_dump(
        force=args.force, max_sets=args.max_sets
    )
    print(f"identity sync ({time.monotonic() - t0:.1f}s): {stats}")

    if args.prices:
        set_ids = await mirror.stale_price_set_ids(limit=args.prices)
        print(f"refreshing prices for {len(set_ids)} sets…")
        for done, sid in enumerate(set_ids, start=1):
            n = await mirror.refresh_set_prices(sid)
            print(f"  [{done}/{len(set_ids)}] {sid}: {n} cards priced")

    print("status:", await mirror.mirror_status())
    return 0 if stats.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
