"""The metrics registry: counting once, and refusing to count anything private.

Two halves. The first is ordinary - a counter counts, a histogram buckets, the
exposition format parses. The second is the half worth writing: **a metric must
stay safe when somebody who may watch the fleet but may not read any tenant's
business scrapes it.** So the assertions are mostly about what does not appear.
"""

from __future__ import annotations

import re

import pytest

from app.observability import names
from app.observability.instruments import Instruments, NullInstruments
from app.observability.metrics import (
    MAX_SERIES_PER_METRIC,
    OVERFLOW,
    MetricRegistry,
)

# Never anywhere: identity, per-execution ids, and anything from a payload.
FORBIDDEN = (
    "organization_id",
    "organization",
    "tenant",
    "user_id",
    "workflow_run_id",
    "tool_execution_id",
    "approval_id",
    "request_id",
    "idempotency",
    "api_key",
    "secret",
    "credential",
    "password",
    "prompt",
    "answer",
    "argument",
)

# The names of business *records*. These are checked against most label values
# but deliberately not against `tool` or `route`, because a capability is
# allowed to say what kind of thing it acts on: `cancel_shipment` is the name
# of a tool in the registry, and `/ai/workflows/{id}/runs` is a route template.
# Neither is anybody's shipment. What would be a leak is a *reference* - and
# that is what the identifier test below looks for.
BUSINESS = ("shipment_", "customer_", "invoice_", "abc123", "cust-")

# A label whose value names its own kind - `route`, because a path template
# spells out the parameters it stands in for, and `tool`, for the same reason.
SELF_DESCRIBING = frozenset({"route", "tool"})


def busy() -> Instruments:
    """Instruments that have seen one of everything."""
    instruments = Instruments()
    instruments.record_http(
        method="POST", route="/api/v1/ai/runs/{run_id}", status_code=200, seconds=0.2
    )
    instruments.record_agent_run(status="completed", milliseconds=1_200)
    instruments.record_llm_call(
        model="claude-opus-5",
        operation="generate",
        outcome="success",
        milliseconds=900,
        input_tokens=11,
        output_tokens=7,
    )
    instruments.record_llm_retry(error_code="llm_rate_limited")
    instruments.record_tool_execution(
        tool="cancel_shipment", outcome="succeeded", safety="destructive", milliseconds=12
    )
    instruments.record_workflow_run(status="succeeded", milliseconds=3_000)
    instruments.record_workflow_step(step_type="tool_call", status="succeeded")
    instruments.record_approval_decision(decision="approved", waited_seconds=3_600)
    instruments.record_error(error_code="tool_failed", layer="tool")
    return instruments


# -- Counting -----------------------------------------------------------------


def test_a_counter_counts_once_per_call() -> None:
    instruments = Instruments()

    instruments.record_agent_run(status="completed", milliseconds=1)

    assert instruments.agent_runs.value("completed") == 1
    assert instruments.agent_run_duration.count("completed") == 1


def test_recording_twice_counts_twice() -> None:
    instruments = Instruments()

    instruments.record_agent_run(status="failed", milliseconds=1)
    instruments.record_agent_run(status="failed", milliseconds=1)

    assert instruments.agent_runs.value("failed") == 2


def test_a_duration_is_observed_once_and_converted_to_seconds() -> None:
    instruments = Instruments()

    instruments.record_agent_run(status="completed", milliseconds=2_500)

    assert instruments.agent_run_duration.count("completed") == 1
    assert instruments.agent_run_duration.sum("completed") == pytest.approx(2.5)


def test_tokens_are_counted_by_direction() -> None:
    instruments = Instruments()

    instruments.record_llm_call(
        model="m",
        operation="generate",
        outcome="success",
        milliseconds=10,
        input_tokens=100,
        output_tokens=25,
    )

    assert instruments.llm_tokens.value("m", "input") == 100
    assert instruments.llm_tokens.value("m", "output") == 25


def test_a_call_that_reported_no_usage_creates_no_token_series() -> None:
    """A row of zeroes looks like data. An absent series is the truth."""
    instruments = Instruments()

    instruments.record_llm_call(model="m", operation="generate", outcome="failed", milliseconds=10)

    assert instruments.llm_tokens.value("m", "input") == 0
    assert "aiops_llm_tokens_total{" not in instruments.render()


def test_a_retry_is_not_a_second_call() -> None:
    instruments = Instruments()

    instruments.record_llm_retry(error_code="llm_timeout")
    instruments.record_llm_call(model="m", operation="generate", outcome="success", milliseconds=10)

    assert instruments.llm_calls.value("m", "generate", "success") == 1
    assert instruments.llm_retries.value("llm_timeout") == 1


def test_an_approval_that_nobody_decided_records_no_wait() -> None:
    """An expiry has no decision latency, because nobody decided it."""
    instruments = Instruments()

    instruments.record_approval_decision(decision="expired", waited_seconds=None)

    assert instruments.approval_decisions.value("expired") == 1
    assert instruments.approval_wait.count("expired") == 0


def test_a_counter_cannot_go_down() -> None:
    instruments = Instruments()

    with pytest.raises(ValueError, match="cannot decrease"):
        instruments.agent_runs.increment("completed", amount=-1)


# -- Cardinality --------------------------------------------------------------


def test_a_value_outside_its_vocabulary_becomes_other() -> None:
    """The cardinality defence, and the reason a bug upstream costs one bucket."""
    instruments = Instruments()

    instruments.record_agent_run(status="something-nobody-declared", milliseconds=1)

    assert instruments.agent_runs.value(OVERFLOW) == 1
    assert instruments.agent_runs.value("something-nobody-declared") == 1  # normalises to other


def test_the_number_of_series_is_bounded() -> None:
    """A label whose values are not enumerated still cannot grow without limit."""
    instruments = Instruments()

    for index in range(MAX_SERIES_PER_METRIC + 50):
        instruments.llm_retries.increment(f"code_{index}")

    rendered = [
        line
        for line in instruments.render().splitlines()
        if line.startswith("aiops_llm_retries_total{")
    ]
    assert len(rendered) == MAX_SERIES_PER_METRIC, "the stated bound is the actual bound"
    assert instruments.llm_retries.value(OVERFLOW) >= 50


def test_the_wrong_number_of_labels_is_a_programming_error() -> None:
    instruments = Instruments()

    with pytest.raises(ValueError, match="label"):
        instruments.agent_runs.increment("completed", "extra")


# -- What must never be in there ----------------------------------------------


def test_no_label_value_carries_a_tenant_an_execution_or_a_payload() -> None:
    """Checked against label *values*, which is where a leak would actually be.

    Not a substring scan over the whole exposition: the route label is a path
    *template*, so ``/api/v1/ai/runs/{run_id}`` legitimately contains the words
    "run_id" while carrying no run id at all. The placeholder is the point - it
    is what stops the real one appearing.
    """
    for name, value in _label_pairs(busy().render()):
        for forbidden in FORBIDDEN:
            assert forbidden not in name.lower(), f"label name {name!r}"
            if name in SELF_DESCRIBING:
                continue
            assert forbidden not in value.lower(), f"label {name}={value!r}"

        for business in BUSINESS:
            if name in SELF_DESCRIBING:
                continue
            assert business not in value.lower(), f"label {name}={value!r}"


def test_run_id_is_never_a_label_name_however_route_templates_read() -> None:
    """The exception above is for templates, and only for templates."""
    names_used = {name for name, _ in _label_pairs(busy().render())}

    assert "run_id" not in names_used
    assert "organization_id" not in names_used


def test_no_label_value_is_an_identifier() -> None:
    """The unbounded-cardinality failure, stated as what it would look like."""
    for name, value in _label_pairs(busy().render()):
        assert not _UUID.fullmatch(value), f"label {name} carries an id: {value}"


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_LABEL = re.compile(r'(\w+)="([^"]*)"')


def _label_pairs(rendered: str) -> list[tuple[str, str]]:
    """Every label name and value in an exposition, ignoring comments."""
    return [
        pair
        for line in rendered.splitlines()
        if line and not line.startswith("#")
        for pair in _LABEL.findall(line)
    ]


def test_the_declared_label_names_are_the_only_ones() -> None:
    """Read from the registry rather than the render, so an unused metric counts."""
    instruments = Instruments()

    declared = {
        label
        for name in instruments.registry.names()
        for label in getattr(instruments.registry.get(name), "labelnames", ())
    }

    assert declared == {
        "method",
        "route",
        "status",
        "model",
        "operation",
        "outcome",
        "direction",
        "error_code",
        "tool",
        "safety",
        "step_type",
        "decision",
        "layer",
    }
    assert "organization_id" not in declared


def test_a_route_label_is_a_template_not_a_path() -> None:
    """Asserted here as a vocabulary property; the middleware enforces it."""
    instruments = Instruments()

    instruments.record_http(
        method="GET", route="/api/v1/ai/runs/{run_id}", status_code=200, seconds=0.1
    )

    rendered = instruments.render()
    assert "{run_id}" in rendered.replace('le="', ""), "the template is what is labelled"


def test_a_status_class_is_recorded_rather_than_a_code() -> None:
    instruments = Instruments()

    instruments.record_http(method="GET", route="/x", status_code=503, seconds=0.1)

    assert instruments.http_requests.value("GET", "/x", "5xx") == 1
    assert names.status_class(200) == "2xx"
    assert names.status_class(418) == "4xx"
    assert names.status_class(999) == "5xx", "an impossible code is still bounded"


# -- Exposition ---------------------------------------------------------------


def test_the_exposition_names_and_types_every_metric() -> None:
    rendered = busy().render()

    for name in Instruments().registry.names():
        assert f"# HELP {name} " in rendered
        assert f"# TYPE {name} " in rendered


def test_a_histogram_renders_buckets_a_sum_and_a_count() -> None:
    instruments = Instruments()
    instruments.record_tool_execution(
        tool="get_shipment", outcome="succeeded", safety="read_only", milliseconds=30
    )

    rendered = instruments.render()

    assert (
        'aiops_tool_execution_duration_seconds_bucket{tool="get_shipment",le="+Inf"} 1' in rendered
    )
    assert 'aiops_tool_execution_duration_seconds_count{tool="get_shipment"} 1' in rendered
    assert "aiops_tool_execution_duration_seconds_sum{" in rendered


def test_every_exposition_line_is_a_comment_or_a_sample() -> None:
    """A cheap parse: anything else would break a scraper."""
    for line in busy().render().splitlines():
        if not line:
            continue
        assert line.startswith("#") or " " in line, line


# -- Isolation between applications -------------------------------------------


def test_two_registries_do_not_share_numbers() -> None:
    """Why this is not a process-wide singleton: tests build many applications."""
    first, second = Instruments(), Instruments()

    first.record_agent_run(status="completed", milliseconds=1)

    assert first.agent_runs.value("completed") == 1
    assert second.agent_runs.value("completed") == 0


def test_null_instruments_keep_nothing_and_never_raise() -> None:
    instruments = NullInstruments()

    instruments.record_agent_run(status="completed", milliseconds=1)
    instruments.record_error(error_code="x", layer="agent")

    assert instruments.render() == ""


def test_a_metric_cannot_be_registered_twice() -> None:
    """Two metrics under one name means one of them is not being read."""
    registry = MetricRegistry()
    registry.counter("x_total", "help")

    with pytest.raises(ValueError, match="already registered"):
        registry.counter("x_total", "help")
