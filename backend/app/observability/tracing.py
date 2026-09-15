"""Tracing: the shape of a request, and nothing about its contents.

Part 19 built metrics and deferred tracing to this phase, "along with somewhere
to send it". This is that module, and the first decision it makes is about what
a span is *for*. A metric answers "how much, how often, how slow" across a
fleet. A trace answers "what happened during this one request, and where did
the time go" - which is a question about a single execution, and therefore the
question most likely to be answered by accidentally attaching that execution's
data to it.

So the design here is the metric design again, tightened:

**Attributes are an allow-list.** ``app.observability.names.SPAN_ATTRIBUTES``
is the complete set. Anything else is *dropped* - not truncated, not hashed,
not logged - before a span exists. Putting a prompt, a tool argument, a tenant
id or a run id into a trace therefore requires editing that file, which is the
one file a reviewer already opens to ask this question.

**Span names come from bounded pieces.** ``GET /api/v1/ai/runs/{run_id}`` is
one span name; ``GET /api/v1/ai/runs/<a uuid>`` would be one per run. The
middleware passes the matched route *template*, exactly as it does for metrics.

**Nothing here can break a request.** Emitting a span is wrapped so that no
exporter failure escapes: ``_finish`` catches ``Exception`` and returns. A
collector that is down, slow, full or absent changes nothing about agent
execution, workflow execution, approvals or ordinary HTTP - by construction,
not by operational care. ``BaseException`` is deliberately *not* caught, so
cancellation and process shutdown still travel.

**Off by default.** ``TRACING_ENABLED`` is false, and while it is false the
tracer is ``NullTracer``: no span is emitted, no context variable is set, no
log line is written, and the correlation id behaves exactly as it did before
this module existed.

What this module is *not*: an OpenTelemetry SDK installation. The platform's
observability has no third-party dependency and this keeps that property. What
it does instead is speak OpenTelemetry's data model - W3C ``traceparent`` in
and out, 16-byte trace ids and 8-byte span ids as lower-case hex, the semantic
convention attribute names, ``unset``/``ok``/``error`` status, a span kind - so
that what it emits maps onto OTLP one-for-one, and the place an OTLP exporter
plugs in is a single ``_emit`` method on a ``Tracer`` subclass.
``docs/deployment.md`` writes that out.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Final, Literal

from app.observability import names

logger = logging.getLogger(__name__)

TRACEPARENT_HEADER: Final = "traceparent"
"""The W3C Trace Context header this application reads."""

_SUPPORTED_VERSION: Final = "00"

_TRACE_ID_HEX: Final = 32
_SPAN_ID_HEX: Final = 16
_INVALID_TRACE_ID: Final = "0" * _TRACE_ID_HEX
_INVALID_SPAN_ID: Final = "0" * _SPAN_ID_HEX

_HEX_DIGITS: Final = frozenset("0123456789abcdef")

SpanStatus = Literal["unset", "ok", "error"]
SpanKind = Literal["server", "internal", "client"]

AttributeValue = str | int | float | bool

_UUID_SHAPED: Final = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
"""Every identifier in this platform is a UUID, so this is what one looks like."""


def _is_hex(value: str, length: int) -> bool:
    """Lower-case hex of exactly ``length`` digits.

    Upper case is rejected rather than folded. The specification says the
    header is lower-case hex, and a parser that quietly accepts a second
    spelling is a parser that can produce two trace ids for one trace.
    """
    return len(value) == length and _HEX_DIGITS.issuperset(value)


@dataclass(frozen=True, slots=True)
class TraceContext:
    """One position in a trace: which trace, which span, and whether recorded."""

    trace_id: str
    span_id: str
    sampled: bool

    def traceparent(self) -> str:
        """This context, as a W3C ``traceparent`` header value."""
        flags = "01" if self.sampled else "00"
        return f"{_SUPPORTED_VERSION}-{self.trace_id}-{self.span_id}-{flags}"

    def child(self) -> TraceContext:
        """A new span in the same trace, inheriting the sampling decision."""
        return TraceContext(trace_id=self.trace_id, span_id=new_span_id(), sampled=self.sampled)


def new_trace_id() -> str:
    return secrets.token_hex(_TRACE_ID_HEX // 2)


def new_span_id() -> str:
    return secrets.token_hex(_SPAN_ID_HEX // 2)


def parse_traceparent(raw: str | None) -> TraceContext | None:
    """Read an incoming ``traceparent``, or ``None`` if it is not usable.

    Strict on purpose. This header arrives from outside and is the one piece of
    trace data a client controls, so everything it produces is validated before
    it can become a trace id in an exported span: the version must be one this
    code understands, both ids must be the right length in lower-case hex, and
    neither may be the all-zero id the specification reserves for "invalid".

    A malformed header starts a fresh trace rather than failing the request - a
    caller with a broken tracing library should not lose service over it.
    """
    if not raw:
        return None

    parts = raw.strip().split("-")
    if len(parts) != 4:
        return None

    version, trace_id, span_id, flags = parts

    # Later versions of the specification may append fields, but this code can
    # only claim to understand the one it was written against - and version 00
    # is defined as exactly four fields, so a fifth means the header did not
    # come from something speaking this version.
    if version != _SUPPORTED_VERSION:
        return None
    if not _is_hex(trace_id, _TRACE_ID_HEX) or trace_id == _INVALID_TRACE_ID:
        return None
    if not _is_hex(span_id, _SPAN_ID_HEX) or span_id == _INVALID_SPAN_ID:
        return None
    if not _is_hex(flags, 2):
        return None

    return TraceContext(trace_id=trace_id, span_id=span_id, sampled=bool(int(flags, 16) & 0x01))


def sampled_by_ratio(trace_id: str, ratio: float) -> bool:
    """Whether a trace is recorded, decided from its id rather than a coin.

    Deterministic on the trace id, which is what makes the decision *consistent*
    between services: every process that sees this trace reaches the same answer
    without having to be told. The alternative - a fresh random draw per service
    - produces traces with holes in them, which are worse than no traces.
    """
    if ratio >= 1.0:
        return True
    if ratio <= 0.0:
        return False
    # The low eight hex digits, as a fraction of their range. Trace ids are
    # random, so any fixed slice is uniform; the low end is the conventional one.
    return int(trace_id[-8:], 16) / 0x1_0000_0000 < ratio


def clean_attributes(
    attributes: Mapping[str, object] | None,
) -> dict[str, AttributeValue]:
    """Keep the attributes a span is permitted to carry, and drop the rest.

    Four filters, in order, and every one of them discards rather than repairs:

    1. the key must be in ``names.SPAN_ATTRIBUTES``;
    2. the value must be a scalar - ``bool``, ``int``, ``float`` or ``str``. A
       dict or a list is the shape a payload has when it arrives somewhere it
       should not be;
    3. a string is bounded at ``names.MAX_ATTRIBUTE_CHARACTERS``. Nothing
       permitted comes close to it, so a value that exceeds it is not truncated
       and shipped - it is dropped, because the interesting fact about it is
       that it should not have been there;
    4. a string containing something UUID-shaped is dropped whatever its key.
       Every identifier in this platform is a UUID - runs, tool executions,
       approvals, conversations, organizations, users - so this is the filter
       that still holds if somebody later puts the wrong key on the allow-list.
       Nothing legitimately on that list can contain one: a model name, a tool
       name, a route template and a lifecycle state are all words.
    """
    if not attributes:
        return {}

    kept: dict[str, AttributeValue] = {}
    for key, value in attributes.items():
        if key not in names.SPAN_ATTRIBUTES:
            continue
        # bool first: it is an int, and True is not a status code.
        if isinstance(value, bool):
            kept[key] = value
        elif isinstance(value, (int, float)):
            kept[key] = value
        elif (
            isinstance(value, str)
            and len(value) <= names.MAX_ATTRIBUTE_CHARACTERS
            and not _UUID_SHAPED.search(value)
        ):
            kept[key] = value
    return kept


@dataclass(slots=True)
class Span:
    """One timed operation.

    Mutable while open, because the two things worth recording - the matched
    route and the response status - are only known once the work is done.
    """

    name: str
    kind: SpanKind
    context: TraceContext
    parent_span_id: str | None
    attributes: dict[str, AttributeValue] = field(default_factory=dict)
    status: SpanStatus = "unset"
    recording: bool = True

    def set_name(self, name: str) -> None:
        """Rename the span once a bounded name for it is known."""
        if self.recording:
            self.name = name

    def set_attribute(self, key: str, value: object) -> None:
        """Add one attribute, if it is one this application publishes."""
        if self.recording:
            self.attributes.update(clean_attributes({key: value}))

    def set_attributes(self, attributes: Mapping[str, object]) -> None:
        if self.recording:
            self.attributes.update(clean_attributes(attributes))

    def set_status(self, status: SpanStatus) -> None:
        if self.recording:
            self.status = status


class Tracer:
    """Builds spans, enforces the vocabulary, and never lets an exporter out.

    A subclass implements ``_emit`` and nothing else. Everything that makes a
    span safe - the allow-list, the swallowed exporter failure, the sampling
    decision - lives here, where writing a new exporter cannot bypass it.
    """

    enabled: bool = False

    def should_sample(self, parent: TraceContext | None) -> bool:
        """Whether a trace starting here is recorded."""
        return False

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind = "internal",
        parent: TraceContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> Iterator[Span]:
        """Open a span, hand it to the caller, and emit it when the block ends.

        An exception inside the block marks the span failed and is re-raised
        unchanged: a trace records *that* the request failed; it does not alter
        how it failed.
        """
        current = Span(
            name=name,
            kind=kind,
            context=parent.child() if parent else self.new_context(parent),
            parent_span_id=parent.span_id if parent else None,
            attributes=clean_attributes(attributes),
        )
        started = perf_counter()
        try:
            yield current
        except BaseException:
            current.set_status("error")
            raise
        finally:
            self._finish(current, seconds=perf_counter() - started)

    def new_context(self, parent: TraceContext | None = None) -> TraceContext:
        """Start a trace here, because nothing upstream did."""
        trace_id = new_trace_id()
        return TraceContext(
            trace_id=trace_id, span_id=new_span_id(), sampled=self.should_sample(parent)
        )

    def _finish(self, span: Span, *, seconds: float) -> None:
        """Hand a finished span to the exporter, whatever the exporter does.

        This is the guarantee the rest of the platform rests on: telemetry
        cannot fail a request. ``Exception`` is caught and dropped to a debug
        line - an exporter broken on every request would otherwise fill the log
        with one repeated traceback. ``BaseException`` is not caught, so
        ``CancelledError`` and ``KeyboardInterrupt`` still travel.
        """
        if not self.enabled or not span.context.sampled:
            return
        if span.status == "unset":
            span.set_status("ok")
        try:
            self._emit(span, seconds=seconds)
        except Exception:  # pragma: no cover - defensive; see the docstring
            logger.debug("Dropping a span: the exporter raised", exc_info=True)

    def _emit(self, span: Span, *, seconds: float) -> None:
        raise NotImplementedError


NON_RECORDING_SPAN: Final = Span(
    name="",
    kind="internal",
    context=TraceContext(trace_id=_INVALID_TRACE_ID, span_id=_INVALID_SPAN_ID, sampled=False),
    parent_span_id=None,
    recording=False,
)
"""The one span a disabled tracer hands out.

Shared deliberately, and inert because ``recording`` is false: every setter on
it returns without touching anything, so handing the same object to every
request in the process cannot accumulate a single attribute. OpenTelemetry does
the same thing under the name ``NonRecordingSpan``, for the same reason - a
call site should not have to ask whether tracing is on before describing what
it is doing.
"""


class NullTracer(Tracer):
    """The default. Records nothing, so a call site needs no branch.

    ``span`` is overridden rather than inherited so that a disabled deployment
    pays nothing at all: no dataclass, no two calls to the random number
    generator, no context manager bookkeeping beyond the generator itself.
    """

    enabled = False

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind = "internal",
        parent: TraceContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> Iterator[Span]:
        yield NON_RECORDING_SPAN


class LoggingTracer(Tracer):
    """Emit each finished span as one structured log record.

    Not a substitute for a collector, and documented as one that is not. It is
    what a deployment with no tracing backend can have for nothing: the records
    carry OpenTelemetry's own field names and id formats, so a collector's
    filelog receiver reads them directly, and until somebody stands one up they
    are still the per-request timing that answers "where did it go".

    The cost is one log line per recorded request, which is what the sample
    ratio is for.
    """

    enabled = True

    def __init__(self, *, ratio: float = 1.0, resource: Mapping[str, object] | None = None) -> None:
        self.ratio = ratio
        self.resource = clean_attributes(resource)

    def should_sample(self, parent: TraceContext | None) -> bool:
        """Honour an upstream decision; otherwise decide from the trace id.

        A parent that says "recorded" is respected even below the local ratio.
        Dropping a span whose parent is being recorded is what produces a trace
        with a hole in the middle of it.
        """
        if parent is not None:
            return parent.sampled
        return self.ratio >= 1.0

    def new_context(self, parent: TraceContext | None = None) -> TraceContext:
        if parent is not None:
            return parent.child()
        trace_id = new_trace_id()
        return TraceContext(
            trace_id=trace_id,
            span_id=new_span_id(),
            sampled=sampled_by_ratio(trace_id, self.ratio),
        )

    def _emit(self, span: Span, *, seconds: float) -> None:
        logger.info(
            "span %s",
            span.name,
            extra={
                "context": {
                    "trace_id": span.context.trace_id,
                    "span_id": span.context.span_id,
                    "parent_span_id": span.parent_span_id,
                    "span_name": span.name,
                    "span_kind": span.kind,
                    "span_status": span.status,
                    "duration_ms": round(seconds * 1000, 2),
                    **self.resource,
                    **span.attributes,
                }
            },
        )


def build_tracer(
    *,
    enabled: bool,
    ratio: float = 1.0,
    resource: Mapping[str, object] | None = None,
) -> Tracer:
    """The tracer this deployment uses.

    One call, so that "is tracing on?" is answered once at start-up rather than
    at every call site. Never raises: a tracer that refused to be built would
    be a telemetry setting that stopped the application from serving.
    """
    if not enabled:
        return NullTracer()
    return LoggingTracer(ratio=ratio, resource=resource)


# -- The ambient trace ---------------------------------------------------------
#
# A span is only useful if the work happening underneath it becomes its
# children, and the alternative to an ambient context is threading a tracer
# through every constructor between the middleware and a tool call - seven
# signatures, in files this phase was told not to redesign, to carry a value
# that is the same for the whole request.
#
# So the tracer and the currently-open span live in context variables, which is
# what OpenTelemetry calls implicit context propagation and what this codebase
# already does with the correlation id. Each request task gets its own copy of
# the context, so two concurrent requests cannot see each other's span, and a
# background script that never set one gets the null tracer and pays nothing.

_current_tracer: ContextVar[Tracer | None] = ContextVar("tracer", default=None)
_current_context: ContextVar[TraceContext | None] = ContextVar("trace_context", default=None)

_NULL_TRACER: Final = NullTracer()


def use_tracer(tracer: Tracer) -> None:
    """Bind a tracer to the current context, for the rest of this request."""
    _current_tracer.set(tracer)


def current_tracer() -> Tracer:
    """The tracer for this request, or one that records nothing."""
    return _current_tracer.get() or _NULL_TRACER


def current_trace_context() -> TraceContext | None:
    """The span currently open, if any."""
    return _current_context.get()


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = "internal",
    parent: TraceContext | None = None,
    attributes: Mapping[str, object] | None = None,
) -> Iterator[Span]:
    """Time a piece of work as a child of whatever is already open.

    The call every layer outside the middleware uses. It asks nothing of its
    caller: no tracer argument, no parent argument, no branch on whether
    tracing is enabled. With tracing off it reads one context variable and
    hands back the shared non-recording span, which is as close to free as a
    context manager gets.
    """
    tracer = current_tracer()
    if not tracer.enabled:
        yield NON_RECORDING_SPAN
        return

    with tracer.span(
        name, kind=kind, parent=parent or _current_context.get(), attributes=attributes
    ) as opened:
        token = _current_context.set(opened.context)
        try:
            yield opened
        finally:
            _current_context.reset(token)
