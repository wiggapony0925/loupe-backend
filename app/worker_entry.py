"""Cloud Run-compatible worker entrypoint.

Cloud Run *services* require the container to listen on ``$PORT`` even though
the arq worker itself does not serve HTTP. We run a minimal background HTTP
responder so the platform health-check passes, and execute the arq worker in
the main thread.

Invoked from Cloud Run with:
    command: ["python"]
    args:    ["-m", "app.worker_entry"]
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from arq.worker import run_worker

from app.worker import WorkerSettings


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_: object) -> None:  # silence default access log
        return


def _serve_health() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    server.serve_forever()


def main() -> None:
    threading.Thread(target=_serve_health, daemon=True).start()
    run_worker(WorkerSettings)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
