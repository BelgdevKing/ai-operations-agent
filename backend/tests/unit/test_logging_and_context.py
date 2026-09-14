"""Structured logging, the correlation id, and the request context middleware."""

from __future__ import annotations

import json
import logging

from httpx import AsyncClient

from app.core.context import get_request_id, reset_request_id, set_request_id
from app.core.logging import ConsoleFormatter, JsonFormatter, configure_logging
from app.core.middleware import REQUEST_ID_HEADER


def _record(message: str = "hello") -> logging.LogRecord:
    """Build a log record directly, so formatters are tested in isolation."""
    return logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=None,
        exc_info=None,
    )


# -- Context ------------------------------------------------------------------


def test_request_id_defaults_to_none() -> None:
    assert get_request_id() is None


def test_request_id_can_be_set_and_restored() -> None:
    token = set_request_id("abc123")
    assert get_request_id() == "abc123"

    reset_request_id(token)
    assert get_request_id() is None


# -- JSON formatter -----------------------------------------------------------


def test_json_formatter_emits_one_parsable_object() -> None:
    payload = json.loads(JsonFormatter().format(_record("something happened")))

    assert payload["message"] == "something happened"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["timestamp"].endswith("+00:00")


def test_json_formatter_includes_the_request_id() -> None:
    token = set_request_id("req-42")
    try:
        payload = json.loads(JsonFormatter().format(_record()))
    finally:
        reset_request_id(token)

    assert payload["request_id"] == "req-42"


def test_json_formatter_omits_request_id_when_absent() -> None:
    assert "request_id" not in json.loads(JsonFormatter().format(_record()))


def test_json_formatter_includes_extra_context() -> None:
    record = _record()
    record.context = {"tenant_id": "t-1", "duration_ms": 12.5}

    payload = json.loads(JsonFormatter().format(record))

    assert payload["context"] == {"tenant_id": "t-1", "duration_ms": 12.5}


def test_json_formatter_serialises_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record("failed")
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exception"]


# -- Console formatter --------------------------------------------------------


def test_console_formatter_appends_the_request_id() -> None:
    token = set_request_id("req-7")
    try:
        line = ConsoleFormatter().format(_record("readable"))
    finally:
        reset_request_id(token)

    assert "readable" in line
    assert "request_id=req-7" in line


def test_console_formatter_stays_plain_without_context() -> None:
    line = ConsoleFormatter().format(_record("plain"))

    assert line.endswith("plain")


# -- Configuration ------------------------------------------------------------


def test_configure_logging_does_not_accumulate_handlers() -> None:
    """The factory runs per application; handlers must not pile up."""
    configure_logging(level="INFO", log_format="json")
    after_first = len(logging.getLogger().handlers)

    configure_logging(level="INFO", log_format="json")

    assert len(logging.getLogger().handlers) == after_first == 1


def test_configure_logging_selects_the_format() -> None:
    configure_logging(log_format="json")
    assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)

    configure_logging(log_format="console")
    assert isinstance(logging.getLogger().handlers[0].formatter, ConsoleFormatter)


def test_configure_logging_sets_the_level() -> None:
    configure_logging(level="warning")

    assert logging.getLogger().level == logging.WARNING


# -- Middleware ---------------------------------------------------------------


async def test_response_carries_a_request_id(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.headers[REQUEST_ID_HEADER]


async def test_each_request_gets_a_distinct_id(client: AsyncClient) -> None:
    first = await client.get("/health")
    second = await client.get("/health")

    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


async def test_an_upstream_request_id_is_preserved(client: AsyncClient) -> None:
    """A proxy or calling service may already have started the trace."""
    response = await client.get("/health", headers={REQUEST_ID_HEADER: "upstream-id"})

    assert response.headers[REQUEST_ID_HEADER] == "upstream-id"
