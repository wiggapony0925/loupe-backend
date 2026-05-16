# Loupe Backend Overview

Loupe is a card-grading platform powered by a Raspberry Pi-based desktop
scanner.  The scanner captures four angles of a trading card (front, back,
top edge, bottom edge), uploads them to this backend, and receives a
forensic grade report covering:

- **Centering** — how well the print is aligned within the card border.
- **Corners** — wear, whitening, and rounding.
- **Edges** — chipping, dings, and edge whitening.
- **Surface** — scratches, print lines, and texture defects.

The backend persists every scan, computes a perceptual fingerprint for
duplicate detection, surfaces upstream catalog data (Pokémon TCG IO,
Scryfall, YGOPRODeck), and tracks user collections.

## Architecture

```
+--------------+        +-----------------+        +----------------+
|  Loupe iOS   |  HTTPS |  FastAPI (this) |  arq   |  scan worker   |
|  + Scanner   +------->+  /v1/* + /ws    +------->+  grading + FP  |
+--------------+        +--------+--------+        +----------------+
                                  |
                  +---------------+---------------+
                  |               |               |
              Postgres        Redis (cache/      S3 / MinIO
              (async)         pub-sub/arq)       (scan images)
```

All I/O is async; the scan worker is the only place where the slow
forensic pipeline runs.  WebSocket clients receive progress events on
`/ws/scans` as the worker advances.
