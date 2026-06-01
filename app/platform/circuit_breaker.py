"""In-process circuit breaker for outbound provider calls.

Why this exists
---------------
We fan out to several public TCG providers (pokemontcg.io, scryfall,
ygoprodeck, tcgcsv, justtcg, …). When one of them flakes — say
pokemontcg.io starts 404-ing every query for an afternoon — naive
retry/timeout behaviour means every cold ``/cards/trending`` and
``/cards/{id}/canonical`` request pays a full ``_PROVIDER_TIMEOUT_S``
penalty waiting for that provider to fail, even though we *know* it's
been failing for the last 60 seconds.

A circuit breaker turns that knowledge into latency. After
``failure_threshold`` consecutive failures the breaker **opens** and
short-circuits subsequent calls for ``cooldown_s`` seconds — the caller
gets an immediate :class:`CircuitOpenError` instead of waiting on a
doomed request. Once the cooldown elapses the breaker goes
**half-open** and lets a single probe through; success closes the
breaker, failure re-opens it for another cooldown window.

State is held in-process (per worker). That's deliberate — the breaker
is a latency optimisation, not a coordination primitive; if one worker
hasn't seen failures yet it's fine for it to discover them on its own.
Sharing state across workers would require Redis round-trips on every
call and defeat the whole point.

Usage
-----
.. code-block:: python

    from app.platform.circuit_breaker import get_breaker, CircuitOpenError

    breaker = get_breaker("pokemontcg")
    try:
        async with breaker.guard():
            data = await pokemon_tcg.search_cards(q, ...)
    except CircuitOpenError:
        return []  # caller decides how to degrade
    except Exception:
        # breaker has already recorded the failure inside __aexit__
        raise

The ``guard()`` context manager records success on a clean exit and
failure on any exception (including timeouts), so callers don't need to
manually update breaker state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

logger = logging.getLogger("loupe.circuit_breaker")


class CircuitOpenError(Exception):
    """Raised by :meth:`CircuitBreaker.guard` when the breaker is open.

    Distinct exception type so callers can distinguish "we skipped this
    call because the provider has been failing" from a real upstream
    error. The breaker name is attached so log lines stay debuggable.
    """

    def __init__(self, name: str, retry_after_s: float) -> None:
        super().__init__(
            f"circuit breaker '{name}' is open; retry in {retry_after_s:.1f}s"
        )
        self.name = name
        self.retry_after_s = retry_after_s


@dataclass
class CircuitBreaker:
    """Single-provider circuit breaker.

    Three states modelled implicitly via ``opened_at`` + ``failures``:

    * **closed**  — ``opened_at is None``; calls pass through, failures
      increment ``failures``, the threshold trips the breaker open.
    * **open**    — ``opened_at is not None`` and ``now < opened_at +
      cooldown_s``; every ``guard()`` raises immediately.
    * **half-open** — cooldown elapsed; exactly one probe is allowed in.
      Concurrent callers serialize on ``_probe_lock`` so we don't send a
      thundering herd at a still-broken provider. Success closes,
      failure re-opens with a fresh cooldown.
    """

    name: str
    failure_threshold: int = 5
    cooldown_s: float = 60.0

    failures: int = 0
    opened_at: float | None = None
    _probe_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ------------------------------------------------------------------
    # State predicates
    # ------------------------------------------------------------------
    def _is_open(self, now: float) -> bool:
        return self.opened_at is not None and now - self.opened_at < self.cooldown_s

    def _retry_after(self, now: float) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.cooldown_s - (now - self.opened_at))

    # ------------------------------------------------------------------
    # Public state mutators (also used directly by tests)
    # ------------------------------------------------------------------
    def record_success(self) -> None:
        if self.opened_at is not None:
            logger.info("circuit breaker '%s' closed after successful probe", self.name)
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold and self.opened_at is None:
            self.opened_at = time.monotonic()
            logger.warning(
                "circuit breaker '%s' opened after %d consecutive failures; "
                "cooling down for %.0fs",
                self.name,
                self.failures,
                self.cooldown_s,
            )
        elif self.opened_at is not None:
            # Half-open probe failed → restart the cooldown clock.
            self.opened_at = time.monotonic()
            logger.warning(
                "circuit breaker '%s' probe failed; re-opening for %.0fs",
                self.name,
                self.cooldown_s,
            )

    # ------------------------------------------------------------------
    # Async context manager — primary entry point for callers
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def guard(self) -> AsyncIterator[None]:
        """Wrap an upstream call; auto-record success/failure on exit.

        Raises
        ------
        CircuitOpenError
            When the breaker is open (cooldown still active).
        Exception
            Re-raises whatever the wrapped call raised, after recording
            a failure on this breaker.
        """
        now = time.monotonic()
        if self._is_open(now):
            raise CircuitOpenError(self.name, self._retry_after(now))

        # If we're past the cooldown window with opened_at still set,
        # we're half-open. Serialize probes so only one caller tests
        # the upstream at a time; others see the breaker as still open.
        if self.opened_at is not None:
            if self._probe_lock.locked():
                raise CircuitOpenError(self.name, 0.0)
            async with self._probe_lock:
                try:
                    yield
                except Exception:
                    self.record_failure()
                    raise
                else:
                    self.record_success()
            return

        try:
            yield
        except Exception:
            self.record_failure()
            raise
        else:
            self.record_success()


# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------
_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(
    name: str, *, failure_threshold: int = 5, cooldown_s: float = 60.0
) -> CircuitBreaker:
    """Return the named breaker, creating it on first access.

    Thresholds are honoured on creation only — subsequent calls with
    different values are ignored (the breaker should be configured by
    its first caller, who owns the SLO). This keeps call sites simple
    without forcing every module to import a shared config table.
    """
    breaker = _breakers.get(name)
    if breaker is None:
        breaker = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            cooldown_s=cooldown_s,
        )
        _breakers[name] = breaker
    return breaker


def reset_all_breakers() -> None:
    """Reset every registered breaker. Test-only helper."""
    for b in _breakers.values():
        b.record_success()


__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "get_breaker",
    "reset_all_breakers",
]
