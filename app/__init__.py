"""Loupe backend — FastAPI service for hardware-scanner card grading.

The ``loupe-backend`` package powers the Loupe mobile app's server tier:

* **Auth**: Apple & Google Sign-In → first-party JWT access + refresh tokens.
* **Scan ingestion**: 4-angle image uploads via S3 presigned URLs.
* **Grading pipeline**: async background worker runs centering / surface /
  edges / corners analysis and persists a :class:`~app.models.grade.GradedCard`.
* **Card catalog**: search across Pokémon TCG, Scryfall, YGOPRODeck.
* **Collections**: user-curated binders of graded cards.
* **WebSocket progress**: real-time scan job updates pushed via Redis pub/sub.
"""

__version__ = "0.1.0"
