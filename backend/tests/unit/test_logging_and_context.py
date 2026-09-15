"""Structured logging, the correlation id, and the request context middleware."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.context import (
    get_request_id,
    get_trace_id,
    reset_request_id,
    reset_trace_id,
    set_request_id,
    set_trace_id,
)
from app.core.logging import ConsoleFormatter, JsonFormatter, configure_logging
from app.core.middleware import REQUEST_ID_HEADER
from app.main import create_app
from app.observability.tracing import TRACEPARENT_HEADER


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


# -- Tracing in the request path ----------------------------------------------
#
# The middleware's own contribution: a trace id bound to the context, a span
# named from the route template, and - while tracing is off - none of it.


def traced_client(settings: Settings, **overrides: object) -> AsyncClient:
    app = create_app(settings.model_copy(update={"tracing_enabled": True, **overrides}))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@contextmanager
def captured_spans() -> Iterator[list[logging.LogRecord]]:
    """Collect the span records, without relying on the root handler.

    ``caplog`` attaches its handler to the root logger, and ``create_app``
    calls ``configure_logging``, which replaces the root handlers - so a test
    that builds an application loses caplog. Attaching to the tracing logger
    itself is unaffected by that, and is what these tests are asking about.
    """
    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("app.observability.tracing")
    handler = Collector(level=logging.INFO)
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def test_trace_id_defaults_to_none() -> None:
    assert get_trace_id() is None


def test_trace_id_can_be_set_and_restored() -> None:
    token = set_trace_id("0af7651916cd43dd8448eb211c80319c")
    assert get_trace_id() == "0af7651916cd43dd8448eb211c80319c"

    reset_trace_id(token)
    assert get_trace_id() is None


def test_json_formatter_includes_the_trace_id() -> None:
    """What joins a log line to a span without either knowing about the other."""
    token = set_trace_id("0af7651916cd43dd8448eb211c80319c")
    try:
        payload = json.loads(JsonFormatter().format(_record()))
    finally:
        reset_trace_id(token)

    assert payload["trace_id"] == "0af7651916cd43dd8448eb211c80319c"


def test_json_formatter_omits_the_trace_id_when_tracing_is_off() -> None:
    """The one that matters for "off means off": a deployment that has not
    enabled tracing produces the log records it produced before it existed."""
    assert "trace_id" not in json.loads(JsonFormatter().format(_record()))


def test_console_formatter_appends_the_trace_id() -> None:
    token = set_trace_id("0af7651916cd43dd8448eb211c80319c")
    try:
        line = ConsoleFormatter().format(_record())
    finally:
        reset_trace_id(token)

    assert "trace_id=0af7651916cd43dd8448eb211c80319c" in line


async def test_a_request_is_not_traced_unless_tracing_is_enabled(
    settings: Settings, client: AsyncClient
) -> None:
    with captured_spans() as spans:
        await client.get("/api/v1/nothing-here")

    assert settings.tracing_enabled is False
    assert spans == []


async def test_an_enabled_request_records_one_span(
    settings: Settings,
) -> None:
    async with traced_client(settings) as client:
        with captured_spans() as spans:
            await client.get("/health")

    assert len(spans) == 1
    context = spans[0].context
    assert context["span_name"] == "GET /health"
    assert context["span_kind"] == "server"
    assert context["http.response.status_code"] == 200
    assert context["span_status"] == "ok"


async def test_a_span_is_named_by_the_route_template_not_the_path(
    settings: Settings,
) -> None:
    """The same rule as the metric label. A path carries ids; a template does
    not, and one span name per run is how a trace store falls over.

    The template is the one Starlette puts in the scope, which is the route's
    path *within the router it was declared in* - ``/runs/{run_id}`` rather
    than ``/api/v1/ai/runs/{run_id}``. That is the same value the Part 19
    metric label has carried since it was written, and it is asserted here as
    it is rather than as one might wish it: the property this test exists for
    is that no identifier from the request reaches the name, and that holds.
    """
    run_id = "11111111-1111-1111-1111-111111111111"

    async with traced_client(settings) as client:
        with captured_spans() as spans:
            await client.get(f"/api/v1/ai/runs/{run_id}")

    context = spans[0].context
    assert context["span_name"] == "GET /runs/{run_id}"
    assert context["http.route"] == "/runs/{run_id}"
    assert run_id not in str(context)


async def test_an_unmatched_path_never_becomes_a_span_name(
    settings: Settings,
) -> None:
    """A 404's path is whatever somebody typed, which is user input."""
    async with traced_client(settings) as client:
        with captured_spans() as spans:
            await client.get("/definitely/not/a/route/secret-looking-value")

    context = spans[0].context
    assert context["span_name"] == "GET unmatched"
    assert "secret-looking-value" not in str(context)


async def test_an_upstream_trace_is_continued(
    settings: Settings,
) -> None:
    upstream = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent = "00f067aa0ba902b7"

    async with traced_client(settings) as client:
        with captured_spans() as spans:
            await client.get("/health", headers={TRACEPARENT_HEADER: f"00-{upstream}-{parent}-01"})

    context = spans[0].context
    assert context["trace_id"] == upstream
    assert context["parent_span_id"] == parent
    assert context["span_id"] != parent


async def test_a_forged_traceparent_starts_a_fresh_trace_instead_of_failing(
    settings: Settings,
) -> None:
    """The header is the one piece of trace data a client controls, so it is
    validated as input. A broken one costs the caller nothing."""
    async with traced_client(settings) as client:
        with captured_spans() as spans:
            response = await client.get(
                "/health", headers={TRACEPARENT_HEADER: "00-<script>-nope-!!"}
            )

    assert response.status_code == 200
    context = spans[0].context
    assert len(context["trace_id"]) == 32
    assert context["parent_span_id"] is None
    assert "<script>" not in str(context)


async def test_a_traced_request_still_carries_its_correlation_id(
    settings: Settings,
) -> None:
    """Two ids, both present: the trace id is for a trace store, the
    correlation id is what a person quotes in a bug report."""
    async with traced_client(settings) as client:
        response = await client.get("/health")

    assert response.headers[REQUEST_ID_HEADER]


async def test_a_sample_ratio_of_zero_records_nothing(
    settings: Settings,
) -> None:
    async with traced_client(settings, tracing_sample_ratio=0.0) as client:
        with captured_spans() as spans:
            response = await client.get("/health")

    assert response.status_code == 200
    assert spans == []
