"""LoupeLogger: colored console + structured JSON line + rotating file."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

_CONFIGURED = False
_LOG_DIR = Path("logs")

_LEVEL_COLORS = {
    "DEBUG": "\033[37m",
    "INFO": "\033[36m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[41m",
}
_RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """Format console log records with ANSI colors when a TTY is available."""

    def __init__(self, use_color: bool) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self.use_color:
            color = _LEVEL_COLORS.get(record.levelname, "")
            return f"{color}{text}{_RESET}"
        return text


class JsonFormatter(logging.Formatter):
    """Format a log record as a single JSON line.

    Any caller may attach extra structured fields via the stdlib ``extra=``
    kwarg (e.g. ``logger.info("msg", extra={"event": {...}})``); the keys
    are merged into the top-level JSON payload, with reserved keys (``ts``,
    ``level``, ``logger``, ``msg``, ``exc``) winning to keep the schema
    stable. Request-scoped fields (request_id, user_id) are auto-injected
    from :mod:`app.platform.request_context` when set.
    """

    _RESERVED = {"ts", "level", "logger", "msg", "exc"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Pull request-scoped context lazily to avoid an import cycle at
        # module load (request_context imports nothing from us).
        try:
            from app.platform.request_context import get_request_id, get_request_user_id

            req_id = get_request_id()
            if req_id:
                payload["request_id"] = req_id
            uid = get_request_user_id()
            if uid:
                payload["user_id"] = uid
        except Exception:  # pragma: no cover - context optional
            pass
        # Merge any extra=... fields supplied by the caller.
        for key, value in record.__dict__.items():
            if (
                key in self._RESERVED
                or key.startswith("_")
                or key
                in {
                    "args",
                    "asctime",
                    "created",
                    "exc_info",
                    "exc_text",
                    "filename",
                    "funcName",
                    "levelname",
                    "levelno",
                    "lineno",
                    "message",
                    "module",
                    "msecs",
                    "msg",
                    "name",
                    "pathname",
                    "process",
                    "processName",
                    "relativeCreated",
                    "stack_info",
                    "thread",
                    "threadName",
                    "taskName",
                }
            ):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class LoupeLogger:
    """Thin façade around :mod:`logging` configured once per process."""

    @staticmethod
    def configure(level: str | int = "INFO", *, json_console: bool = False) -> None:
        """Configure root logging handlers. Safe to call multiple times."""
        global _CONFIGURED
        if _CONFIGURED:
            return

        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)

        lvl = (
            logging._nameToLevel.get(str(level).upper(), logging.INFO)
            if isinstance(level, str)
            else level
        )
        root.setLevel(lvl)

        # --- Console handler ---
        console = logging.StreamHandler(stream=sys.stdout)
        if json_console:
            console.setFormatter(JsonFormatter())
        else:
            console.setFormatter(ColorFormatter(use_color=sys.stdout.isatty()))
        root.addHandler(console)

        # --- Rotating file (best-effort; skip if unwritable e.g. read-only FS) ---
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                _LOG_DIR / "loupe.log",
                maxBytes=5_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            fh.setFormatter(JsonFormatter())
            root.addHandler(fh)
        except Exception:
            pass

        _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, configuring the root once on first call."""
    if not _CONFIGURED:
        level = os.environ.get("LOG_LEVEL", "INFO")
        json_console = os.environ.get("LOG_JSON", "false").lower() in {
            "1",
            "true",
            "yes",
        }
        LoupeLogger.configure(level=level, json_console=json_console)
    return logging.getLogger(f"loupe.{name}")


__all__ = ["ColorFormatter", "JsonFormatter", "LoupeLogger", "get_logger"]
