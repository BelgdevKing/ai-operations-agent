"""Spans: what they may carry, who decides to record one, and what they cannot do.

Three claims are worth more than the rest of this file put together, and they
are the ones a production deployment is trusting:

1. **A span cannot carry something it should not.** The attribute allow-list is
   enforced in one place and nothing routes around it - not the constructor,
   not ``set_attribute``, not an exporter written later.
2. **A span cannot break a request.** An exporter that raises on every call
   changes nothing about the response.
3. **Off means off.** With tracing disabled nothing is built, nothing is
   emitted, and the shared non-recording span cannot accumulate state across
   the requests that all receive it.

The header parsing tests are the fourth: ``traceparent`` is the one piece of
trace data a client supplies, so it is treated as input, not as configuration.
"""

from __future__ import annotations

import logging

import pytest

from app.observability import names
from app.observability.tracing import (
    NON_RECORDING_SPAN,
    LoggingTracer,
    NullTracer,
    Span,
    TraceContext,
    Tracer,
    build_tracer,
    clean_attributes,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    sampled_by_ratio,
)

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN = "00f067aa0ba902b7"


def traceparent(trace: str = TRACE, span: str = SPAN, flags: str = "01") -> str:
    return f"00-{trace}-{span}-{flags}"


class ExplodingTracer(Tracer):
    """An exporter that fails every time, which is the case that matters."""

    enabled = True
    calls = 0

    def should_sample(self, parent: TraceContext | None) -> bool:
        return True

    def _emit(self, span: Span, *, seconds: float) -> None:
        type(self).calls += 1
        raise RuntimeError("the collector is on fire")


class RecordingTracer(Tracer):
    """Keeps finished spans in a list, so a test can look at them."""

    enabled = True

    def __init__(self) -> None:
        self.spans: list[Span] = []

    def should_sample(self, parent: TraceContext | None) -> bool:
        return parent.sampled if parent else True

    def _emit(self, span: Span, *, seconds: float) -> None:
        self.spans.append(span)


# -- Ids ----------------------------------------------------------------------


def test_ids_are_the_lengths_the_specification_requires() -> None:
    assert len(new_trace_id()) == 32
    assert len(new_span_id()) == 16


def test_ids_are_lower_case_hex() -> None:
    for generated in (new_trace_id(), new_span_id()):
        assert generated == generated.lower()
        assert int(generated, 16) >= 0


def test_ids_are_not_predictable() -> None:
    """Generated from ``secrets``: a guessable span id lets somebody graft a
    span onto a trace they are not part of."""
    assert len({new_trace_id() for _ in range(64)}) == 64


# -- Reading an incoming traceparent ------------------------------------------


def test_a_well_formed_header_continues_the_trace() -> None:
    parsed = parse_traceparent(traceparent())

    assert parsed is not None
    assert parsed.trace_id == TRACE
    assert parsed.span_id == SPAN
    assert parsed.sampled is True


def test_the_sampled_flag_is_read_from_the_low_bit() -> None:
    assert parse_traceparent(traceparent(flags="00")).sampled is False  # type: ignore[union-attr]
    assert parse_traceparent(traceparent(flags="01")).sampled is True  # type: ignore[union-attr]
    # Other flag bits are reserved; only the low one means "recorded".
    assert parse_traceparent(traceparent(flags="ff")).sampled is True  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "   ",
        "not-a-traceparent",
        # A version this code has not been written against.
        f"01-{TRACE}-{SPAN}-01",
        # Version 00 is defined as exactly four fields.
        f"00-{TRACE}-{SPAN}-01-extra",
        f"00-{TRACE}-{SPAN}",
        # Wrong lengths.
        f"00-{TRACE[:-1]}-{SPAN}-01",
        f"00-{TRACE}-{SPAN[:-1]}-01",
        # Upper case is a different spelling of the same id, and two spellings
        # of one trace id is two traces.
        f"00-{TRACE.upper()}-{SPAN}-01",
        # Not hex at all.
        f"00-{'z' * 32}-{SPAN}-01",
        # The all-zero ids the specification reserves for "invalid".
        f"00-{'0' * 32}-{SPAN}-01",
        f"00-{TRACE}-{'0' * 16}-01",
        # Flags that are not two hex digits.
        f"00-{TRACE}-{SPAN}-0",
        f"00-{TRACE}-{SPAN}-zz",
    ],
)
def test_an_unusable_header_is_refused_rather_than_repaired(header: str | None) -> None:
    assert parse_traceparent(header) is None


def test_a_broken_header_does_not_cost_the_caller_their_request() -> None:
    """Refusing is ``None``, not an exception. A client with a broken tracing
    library gets served; it just starts a new trace here."""
    assert parse_traceparent("00-garbage") is None


# -- Writing one back ----------------------------------------------------------


def test_a_context_round_trips_through_the_header() -> None:
    original = TraceContext(trace_id=TRACE, span_id=SPAN, sampled=True)

    assert parse_traceparent(original.traceparent()) == original


def test_an_unsampled_context_round_trips_too() -> None:
    original = TraceContext(trace_id=TRACE, span_id=SPAN, sampled=False)

    assert parse_traceparent(original.traceparent()) == original


def test_a_child_keeps_the_trace_and_the_decision_but_not_the_span() -> None:
    parent = TraceContext(trace_id=TRACE, span_id=SPAN, sampled=True)
    child = parent.child()

    assert child.trace_id == parent.trace_id
    assert child.sampled == parent.sampled
    assert child.span_id != parent.span_id


# -- Sampling ------------------------------------------------------------------


def test_a_ratio_of_one_records_everything() -> None:
    assert all(sampled_by_ratio(new_trace_id(), 1.0) for _ in range(32))


def test_a_ratio_of_zero_records_nothing() -> None:
    assert not any(sampled_by_ratio(new_trace_id(), 0.0) for _ in range(32))


def test_the_decision_is_a_function_of_the_trace_id() -> None:
    """Which is what makes it the same decision in every service that sees this
    trace. A per-service coin toss produces traces with holes in them."""
    trace = new_trace_id()

    assert sampled_by_ratio(trace, 0.5) == sampled_by_ratio(trace, 0.5)


def test_a_ratio_thins_the_traces_roughly_as_asked() -> None:
    traces = [new_trace_id() for _ in range(2_000)]
    recorded = sum(sampled_by_ratio(trace, 0.25) for trace in traces)

    # Random ids, so this is a band rather than a number.
    assert 0.18 < recorded / len(traces) < 0.32


def test_a_parent_that_is_being_recorded_is_honoured_below_the_local_ratio() -> None:
    """Half a trace is worse than none of it."""
    tracer = LoggingTracer(ratio=0.0)
    parent = TraceContext(trace_id=TRACE, span_id=SPAN, sampled=True)

    assert tracer.should_sample(parent) is True


def test_a_parent_that_is_not_being_recorded_is_honoured_too() -> None:
    tracer = LoggingTracer(ratio=1.0)
    parent = TraceContext(trace_id=TRACE, span_id=SPAN, sampled=False)

    assert tracer.should_sample(parent) is False


# -- The attribute allow-list --------------------------------------------------


def test_a_permitted_attribute_is_kept() -> None:
    kept = clean_attributes({"http.route": "/api/v1/ai/runs/{run_id}"})

    assert kept == {"http.route": "/api/v1/ai/runs/{run_id}"}


@pytest.mark.parametrize(
    "key",
    [
        "organization_id",
        "organization.id",
        "user.id",
        "run_id",
        "tool_execution_id",
        "approval_id",
        "prompt",
        "messages",
        "tool.arguments",
        "tool.result",
        "authorization",
        "anthropic_api_key",
        "db.statement",
        "http.request.body",
    ],
)
def test_an_attribute_nobody_allowed_never_reaches_a_span(key: str) -> None:
    """The list above is every category the deployment brief forbids on a span:
    tenant identity, execution identity, model input and output, credentials.
    None of them is in ``SPAN_ATTRIBUTES``, so none of them survives."""
    assert clean_attributes({key: "anything at all"}) == {}


def test_a_dropped_attribute_takes_nothing_permitted_with_it() -> None:
    kept = clean_attributes({"http.route": "/health", "prompt": "secret"})

    assert kept == {"http.route": "/health"}


@pytest.mark.parametrize("value", [{"nested": 1}, ["a", "b"], object(), None, b"bytes"])
def test_a_value_that_is_not_a_scalar_is_dropped(value: object) -> None:
    """A dict or a list is the shape a payload has when it has arrived
    somewhere it should not be."""
    assert clean_attributes({"http.route": value}) == {}


def test_an_over_long_string_is_dropped_rather_than_truncated() -> None:
    """Truncating would ship the first 128 characters of whatever it was. The
    interesting fact about an over-long value is that it should not exist."""
    oversized = "x" * (names.MAX_ATTRIBUTE_CHARACTERS + 1)

    assert clean_attributes({"http.route": oversized}) == {}
    assert clean_attributes({"http.route": "x" * names.MAX_ATTRIBUTE_CHARACTERS})


def test_numbers_and_booleans_survive_as_themselves() -> None:
    kept = clean_attributes({"http.response.status_code": 503})

    assert kept == {"http.response.status_code": 503}
    assert isinstance(kept["http.response.status_code"], int)


# -- The vocabulary itself -----------------------------------------------------


def test_no_permitted_attribute_is_an_identifier() -> None:
    """Nothing on the allow-list names a thing rather than describing one.

    An attribute called ``anything.id`` is the one that turns a trace store
    into a record of which tenant did what, and it is also the one that turns
    it into an unbounded set of values.
    """
    for key in names.SPAN_ATTRIBUTES:
        assert not key.endswith((".id", "_id", ".ids", "_ids", ".uuid", ".key")), key


def test_no_permitted_attribute_describes_a_person_or_a_tenant() -> None:
    """These four words are absent from the vocabulary, and must stay absent.

    ``approval.decision`` and ``workflow.status`` are fine - a lifecycle state
    is a closed set of words that describes *what happened*. ``user.email`` or
    ``organization.slug`` would describe *who it happened to*, which is the
    tenant's business data and does not leave this process on a span.

    ``service.name`` and ``deployment.environment.name`` name the deployment
    rather than anybody in it, which is why "name" is not on this list and the
    four words below are.
    """
    for key in names.SPAN_ATTRIBUTES:
        for word in ("organization", "tenant", "user", "conversation"):
            assert word not in key.lower(), key


def test_the_vocabulary_uses_the_namespaces_it_declares_and_no_others() -> None:
    """Every key is ``<namespace>.<thing>``, and the namespaces are a closed set.

    Adding one means adding a whole new subject that traces describe, which is
    a decision worth making on purpose rather than by autocompleting a string.
    """
    namespaces = {key.split(".")[0] for key in names.SPAN_ATTRIBUTES}

    assert namespaces == {
        "service",
        "deployment",
        "http",
        "error",
        "agent",
        "llm",
        "tool",
        "workflow",
        "approval",
    }


def test_an_identifier_cannot_ride_in_on_a_permitted_key() -> None:
    """The filter that holds when the allow-list is wrong.

    Every identifier in this platform is a UUID, so a UUID-shaped value is
    dropped whatever key it arrives under. Nothing legitimately permitted can
    contain one - a model name, a tool name, a route template and a lifecycle
    state are all words - so this costs nothing and catches the mistake that
    the key-level list cannot.
    """
    run_id = "11111111-1111-1111-1111-111111111111"

    assert clean_attributes({"tool.name": run_id}) == {}
    assert clean_attributes({"http.route": f"/runs/{run_id}"}) == {}
    assert clean_attributes({"agent.status": run_id.upper()}) == {}
    # And the legitimate values still pass.
    assert clean_attributes({"tool.name": "get_shipment"}) == {"tool.name": "get_shipment"}


def test_the_allow_list_is_small_enough_to_read() -> None:
    """Not a style rule. The security property here is that a reviewer can hold
    the whole list in their head; a list of forty would be one nobody checks."""
    assert len(names.SPAN_ATTRIBUTES) <= 24


# -- A span's life -------------------------------------------------------------


def test_a_span_that_completes_is_recorded_as_ok() -> None:
    tracer = RecordingTracer()

    with tracer.span("GET /health", kind="server"):
        pass

    assert [span.status for span in tracer.spans] == ["ok"]


def test_a_span_whose_block_raises_is_recorded_as_an_error() -> None:
    tracer = RecordingTracer()

    with pytest.raises(ValueError, match="boom"):
        with tracer.span("POST /api/v1/ai/runs"):
            raise ValueError("boom")

    assert [span.status for span in tracer.spans] == ["error"]


def test_the_exception_is_re_raised_unchanged() -> None:
    """A trace records *that* a request failed. It does not get to alter how."""
    tracer = RecordingTracer()
    original = ValueError("the original")

    with pytest.raises(ValueError) as raised:
        with tracer.span("x"):
            raise original

    assert raised.value is original


def test_cancellation_is_not_swallowed() -> None:
    """``BaseException`` travels. A tracer that ate a ``CancelledError`` would
    turn a graceful shutdown into a hang."""
    tracer = RecordingTracer()

    with pytest.raises(BaseException, match="stopping"):
        with tracer.span("x"):
            raise KeyboardInterrupt("stopping")


def test_a_span_records_the_attributes_set_while_it_was_open() -> None:
    tracer = RecordingTracer()

    with tracer.span("server") as span:
        span.set_name("GET /health")
        span.set_attribute("http.request.method", "GET")
        span.set_attribute("prompt", "not this one")

    recorded = tracer.spans[0]
    assert recorded.name == "GET /health"
    assert recorded.attributes == {"http.request.method": "GET"}


def test_a_child_span_stays_in_its_parents_trace() -> None:
    tracer = RecordingTracer()
    parent = TraceContext(trace_id=TRACE, span_id=SPAN, sampled=True)

    with tracer.span("child", parent=parent):
        pass

    recorded = tracer.spans[0]
    assert recorded.context.trace_id == TRACE
    assert recorded.parent_span_id == SPAN
    assert recorded.context.span_id != SPAN


def test_an_unsampled_span_is_not_emitted() -> None:
    tracer = RecordingTracer()
    parent = TraceContext(trace_id=TRACE, span_id=SPAN, sampled=False)

    with tracer.span("child", parent=parent):
        pass

    assert tracer.spans == []


# -- Telemetry cannot break the application ------------------------------------


def test_an_exporter_that_raises_does_not_reach_the_caller() -> None:
    """The guarantee the platform rests on: a collector outage changes nothing
    about agent execution, workflow execution, approvals or HTTP."""
    tracer = ExplodingTracer()
    ExplodingTracer.calls = 0

    result = None
    with tracer.span("GET /health"):
        result = "the request still finished"

    assert result == "the request still finished"
    assert ExplodingTracer.calls == 1


def test_an_exporter_that_raises_does_not_mask_a_real_failure() -> None:
    tracer = ExplodingTracer()

    with pytest.raises(ValueError, match="the real one"):
        with tracer.span("x"):
            raise ValueError("the real one")


def test_a_failing_exporter_is_quiet_about_it(caplog: pytest.LogCaptureFixture) -> None:
    """Debug, not error: an exporter broken on every request would otherwise
    fill the log with one repeated traceback and bury the real traffic."""
    tracer = ExplodingTracer()

    with caplog.at_level(logging.INFO, logger="app.observability.tracing"):
        with tracer.span("x"):
            pass

    assert caplog.records == []


# -- Off means off -------------------------------------------------------------


def test_tracing_is_off_unless_a_deployment_asks() -> None:
    assert isinstance(build_tracer(enabled=False), NullTracer)
    assert build_tracer(enabled=False).enabled is False


def test_enabling_it_produces_a_tracer_that_records() -> None:
    assert isinstance(build_tracer(enabled=True), LoggingTracer)
    assert build_tracer(enabled=True).enabled is True


def test_a_disabled_tracer_still_hands_out_a_span() -> None:
    """So that a call site never has to ask whether tracing is on."""
    with NullTracer().span("anything") as span:
        span.set_attribute("http.route", "/health")
        span.set_status("error")


def test_the_disabled_span_cannot_accumulate_anything() -> None:
    """It is one shared object handed to every request in the process. If its
    setters wrote, it would grow without limit and leak between tenants."""
    for index in range(100):
        with NullTracer().span("x") as span:
            span.set_attribute("http.route", f"/route/{index}")
            span.set_name("renamed")
            span.set_status("error")

    assert NON_RECORDING_SPAN.attributes == {}
    assert NON_RECORDING_SPAN.name == ""
    assert NON_RECORDING_SPAN.status == "unset"
    assert NON_RECORDING_SPAN.recording is False


def test_a_disabled_tracer_writes_no_log_line(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="app.observability.tracing"):
        with NullTracer().span("GET /health"):
            pass

    assert caplog.records == []


# -- The logging exporter ------------------------------------------------------


def test_a_recorded_span_becomes_one_structured_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracer = LoggingTracer(ratio=1.0, resource={"service.name": "aiops"})

    with caplog.at_level(logging.INFO, logger="app.observability.tracing"):
        with tracer.span("server", kind="server") as span:
            span.set_name("GET /health")
            span.set_attribute("http.response.status_code", 200)

    assert len(caplog.records) == 1
    context = caplog.records[0].context  # type: ignore[attr-defined]
    assert context["span_name"] == "GET /health"
    assert context["span_kind"] == "server"
    assert context["span_status"] == "ok"
    assert context["http.response.status_code"] == 200
    assert context["service.name"] == "aiops"
    assert len(context["trace_id"]) == 32
    assert context["duration_ms"] >= 0


def test_the_resource_goes_through_the_same_allow_list() -> None:
    """Resource attributes are set from configuration, but configuration is
    still not a reason to skip the filter."""
    tracer = LoggingTracer(resource={"service.name": "aiops", "operator.email": "a@b.test"})

    assert tracer.resource == {"service.name": "aiops"}
