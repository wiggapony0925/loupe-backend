"""Cloud Run-compatible worker entrypoint.

Cloud Run *services* require the container to listen on ``$PORT`` even though
the arq worker itself does not serve HTTP. We run a minimal background HTTP
responder so the platform health-check passes, and execute the arq worker in
the main thread.

The worker is SUPERVISED: if arq exits (most commonly because Redis is
unreachable — e.g. the broker was re-provisioned and the URL secret hasn't
landed yet), we log the failure and retry with capped exponential backoff
instead of letting the container die. A bare crash here turns into a Cloud
Run restart loop: alert noise, cold-start billing, and no faster recovery.
With the supervisor the container stays up, reports its degraded state on
``/``, and self-heals the moment the broker is reachable again.

Invoked from Cloud Run with:
    command: ["python"]
    args:    ["-m", "app.worker_entry"]
"""

from __future__ import annotations

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from arq.worker import run_worker

from app.utils.logger import get_logger
from app.worker import WorkerSettings

_log = get_logger("worker.entry")

# Supervisor backoff: 5s → 10s → 20s … capped at 5 min. The cap keeps
# recovery latency reasonable once the broker comes back; the floor stops
# a hard-down broker from being hammered.
_BACKOFF_INITIAL_S = 5.0
_BACKOFF_MAX_S = 300.0

#: Worker state surfaced by the health responder. "running" while arq owns
#: the main thread; "waiting: …" between attempts so an operator curling
#: the service sees WHY jobs aren't moving without opening logs.
_state = {"status": "starting"}


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        # Always 200: the CONTAINER is healthy (the supervisor is alive and
        # will reconnect). Failing the probe here would make Cloud Run
        # recycle the instance — recreating the crash-loop we're avoiding.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(str(_state["status"]).encode())

    def log_message(self, *_: object) -> None:  # silence default access log
        return


def _serve_health() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    server.serve_forever()


def _run_supervised() -> None:
    """Run arq forever, restarting with capped exponential backoff."""
    attempt = 0
    while True:
        _state["status"] = "running"
        started = time.monotonic()
        try:
            run_worker(WorkerSettings)  # type: ignore[arg-type]
            # arq returned cleanly (SIGTERM drain) — exit the supervisor too.
            _log.info("worker exited cleanly; shutting down")
            return
        except Exception:
            # A healthy stretch resets the backoff so one blip after days of
            # uptime doesn't start from a long delay.
            if time.monotonic() - started > _BACKOFF_MAX_S:
                attempt = 0
            attempt += 1
            delay = min(_BACKOFF_INITIAL_S * (2 ** (attempt - 1)), _BACKOFF_MAX_S)
            _state["status"] = (
                f"waiting: worker crashed (attempt {attempt}); retry in {delay:.0f}s"
            )
            _log.exception(
                "worker crashed (attempt %d); retrying in %.0fs", attempt, delay
            )
            time.sleep(delay)


def main() -> None:
    threading.Thread(target=_serve_health, daemon=True).start()
    _run_supervised()


if __name__ == "__main__":
    main()
