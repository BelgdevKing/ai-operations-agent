"""Structured logging.

Two formats are available, chosen by ``LOG_FORMAT``:

``json``
    One JSON object per line, for log aggregators.
``console``
    Compact human-readable lines, for local development.

Both attach the current request's correlation id, so every line produced while
handling a request can be traced back to it, and the trace id alongside it when
tracing is enabled - which is what lets a log line and a span be joined without
either knowing about the other. Extra fields passed through
``logger.info("...", extra={"context": {...}})`` are included in the output.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

from app.core.context import get_request_id, get_trace_id

# Attributes present on every LogRecord; anything else was added by the caller.
# "color_message" is uvicorn's copy of the message with ANSI codes in it, which
# would otherwise show up as duplicated context on every server log line.
_RESERVED: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName", "color_message"}


def _record_context(record: logging.LogRecord) -> dict[str, Any]:
    """Collect caller-supplied ``extra`` fields from a record."""
    context: dict[str, Any] = {}
    explicit = getattr(record, "context", None)
    if isinstance(explicit, dict):
        context.update(explicit)
    for key, value in record.__dict__.items():
        if key not in _RESERVED and key != "context":
            context[key] = value
    return context


class JsonFormatter(logging.Formatter):
    """Render records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        context = _record_context(record)

        # The ambient ids, unless the record carried its own. A span record does
        # carry its own - it has to stand alone, because a span may be emitted
        # from somewhere with no request context around it - and printing the
        # same id twice on one line helps nobody.
        request_id = get_request_id()
        if request_id and "request_id" not in context:
            payload["request_id"] = request_id

        trace_id = get_trace_id()
        if trace_id and "trace_id" not in context:
            payload["trace_id"] = trace_id

        if context:
            payload["context"] = context

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable single-line output with the correlation id appended."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)-28s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)

        context = _record_context(record)

        suffix: list[str] = []
        request_id = get_request_id()
        if request_id and "request_id" not in context:
            suffix.append(f"request_id={request_id}")
        trace_id = get_trace_id()
        if trace_id and "trace_id" not in context:
            suffix.append(f"trace_id={trace_id}")
        suffix.extend(f"{key}={value}" for key, value in context.items())

        return f"{line} [{' '.join(suffix)}]" if suffix else line


def configure_logging(level: str = "INFO", log_format: str = "console") -> None:
    """Install the root logging configuration.

    Safe to call more than once: existing handlers are replaced rather than
    accumulated, which matters because tests build applications repeatedly.
    """
    formatter: logging.Formatter = JsonFormatter() if log_format == "json" else ConsoleFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; let records propagate to ours instead
    # so that everything shares one format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # SQLAlchemy echoes statements at INFO; database_echo controls that instead.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
