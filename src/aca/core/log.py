"""log.py -- Structured logging for ACA.

Provides:
- configure_aca_logging  -- one-shot setup of root ACA logger
- get_logger             -- return a named ACA logger
- ACALogFormatter        -- JSON formatter with run context fields
- RunContext             -- thread-local context manager for run metadata tagging
"""
from __future__ import annotations

import logging
import threading
from typing import Any


# ---------------------------------------------------------------------------
# Thread-local run context
# ---------------------------------------------------------------------------

_context = threading.local()


class RunContext:
    """Thread-local context manager that tags log records with run metadata.

    Usage::

        with RunContext(run_id="run-abc123", phase="planning"):
            logger.info("Manager prompt started")
            # LogRecord will include run_id and phase in its 'extra' fields
    """

    def __init__(self, run_id: str, **kwargs: Any) -> None:
        self._run_id = run_id
        self._extra = kwargs
        self._previous: dict[str, Any] | None = None

    def __enter__(self) -> "RunContext":
        self._previous = getattr(_context, "fields", None)
        _context.fields = {"run_id": self._run_id, **self._extra}
        return self

    def __exit__(self, *args: Any) -> None:
        if self._previous is None:
            _context.fields = {}
        else:
            _context.fields = self._previous


def get_context_fields() -> dict[str, Any]:
    """Return the currently active run context fields (empty dict if none)."""
    return dict(getattr(_context, "fields", None) or {})


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

import json
import time


class ACALogFormatter(logging.Formatter):
    """Format log records as JSON lines with ACA run-context fields.

    Produces records like::

        {"ts": 1712345678.123, "level": "INFO", "logger": "aca.runner",
         "run_id": "run-abc123", "phase": "planning", "msg": "Manager prompt started"}
    """

    def format(self, record: logging.LogRecord) -> str:
        ctx = get_context_fields()
        payload: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
        }
        payload.update(ctx)
        payload["msg"] = record.getMessage()
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Console formatter (human-readable for terminal output)
# ---------------------------------------------------------------------------

_LEVEL_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[35m",
}
_RESET = "\033[0m"


class ACAConsoleFormatter(logging.Formatter):
    """Human-readable formatter for terminal output with optional ANSI color."""

    def __init__(self, color: bool = True) -> None:
        super().__init__()
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        ctx = get_context_fields()
        run_id = ctx.get("run_id", "")
        phase = ctx.get("phase", "")
        prefix_parts = ["aca"]
        if run_id:
            prefix_parts.append(run_id[:12])
        if phase:
            prefix_parts.append(phase)
        prefix = "/".join(prefix_parts)
        level = record.levelname
        color = _LEVEL_COLORS.get(level, "") if self._color else ""
        reset = _RESET if self._color else ""
        line = f"[{prefix}] {color}{level}{reset}: {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

_configured = False


def configure_aca_logging(
    level: str = "INFO",
    *,
    json_output: bool = False,
    color: bool = True,
) -> None:
    """Configure the root ``aca`` logger.

    Call once at process startup (entrypoint, CLI, or API server startup).

    Args:
        level:       Logging level string (e.g. "INFO", "DEBUG").
        json_output: If True emit JSON lines; if False emit human-readable.
        color:       If True use ANSI color in console output (ignored if json_output).
    """
    global _configured
    if _configured:
        return
    _configured = True

    root_logger = logging.getLogger("aca")
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not root_logger.handlers:
        handler = logging.StreamHandler()
        if json_output:
            handler.setFormatter(ACALogFormatter())
        else:
            handler.setFormatter(ACAConsoleFormatter(color=color))
        root_logger.addHandler(handler)

    # Prevent propagation to the root Python logger to avoid duplicate output
    root_logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``aca`` namespace.

    Example::

        logger = get_logger("runner_core")  # -> logging.getLogger("aca.runner_core")
    """
    if not name.startswith("aca."):
        name = f"aca.{name}"
    return logging.getLogger(name)
