"""An in-process metrics registry.

Counters, histograms and gauges, and a renderer that writes them in the
Prometheus text exposition format. About two hundred lines, no dependency, no
I/O, no background thread.

**Why not a library.** ``prometheus-client`` brings a process-global default
registry and multiprocess machinery, and this application builds a fresh
``FastAPI`` through ``create_app(settings)` many times in one test run. A global
registry shared across those applications would accumulate series from every
test and make "this counter incremented exactly once" untestable. A registry
that is an ordinary object, created with the application and thrown away with
it, is the smaller and more honest fit.

**What must never appear in here.** A metric name, a label name or a label
value is written into a time series that an operator scrapes - somebody who may
legitimately watch the fleet without being entitled to any tenant's business
data. So:

* no ``organization_id``, and no other tenant identifier;
* no ``run_id``, ``tool_execution_id``, ``approval_id``, ``request_id`` or any
  other per-execution identifier - they are unbounded, and a metric with
  unbounded labels is a memory leak with a graph on the front;
* no prompts, answers, tool arguments, tool results, business references,
  idempotency keys or credentials, at any level.

Label values come from closed sets declared in :mod:`app.observability.names`.
A value outside its set is recorded as ``other`` rather than admitted, so a bug
upstream costs one bucket instead of unbounded cardinality. A hard ceiling on
series per metric backs that up.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

MAX_SERIES_PER_METRIC = 256
"""Ceiling on distinct label combinations one metric may hold.

Every label in this application is drawn from a closed set, so the ceiling
should be unreachable. It exists because "should be" is not a memory bound: a
future label added carelessly hits this instead of the heap, and the overflow
series makes the mistake visible rather than fatal.
"""

OVERFLOW = "other"
"""Where a value outside its declared set - or past the ceiling - is counted."""

# One slot is reserved for the overflow bucket itself, so a metric holds at
# most MAX_SERIES_PER_METRIC series in total rather than that many plus one.
_CEILING = MAX_SERIES_PER_METRIC - 1

# Seconds. Chosen for what this application actually does: tool calls in
# milliseconds, model calls in seconds, and a long tail for a slow provider.
DURATION_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)

# Seconds, for how long a person took to answer. Hours and days, not seconds:
# an approval that waits a week is the interesting one.
WAIT_BUCKETS: tuple[float, ...] = (
    60.0,
    300.0,
    900.0,
    3_600.0,
    14_400.0,
    43_200.0,
    86_400.0,
    259_200.0,
    604_800.0,
)

LabelValues = tuple[str, ...]


def _escape(value: str) -> str:
    """Escape a label value for the exposition format."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format(value: float) -> str:
    """Render a number the way the exposition format expects."""
    if value == math.inf:
        return "+Inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


@dataclass
class _Metric:
    """Shared state of every metric kind."""

    name: str
    help_text: str
    labelnames: tuple[str, ...] = ()
    #: Allowed values per label, in the same order as ``labelnames``. An empty
    #: frozenset means "any value", which is only ever used for a label whose
    #: values come from a registry the deployment itself controls.
    allowed: tuple[frozenset[str], ...] = ()
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.allowed and len(self.allowed) != len(self.labelnames):
            raise ValueError(f"{self.name}: one allowed set is needed per label.")

    def _key(self, values: Sequence[str]) -> LabelValues:
        """Normalise a label tuple against the declared sets.

        Anything unexpected becomes ``other`` rather than a new series. That is
        the whole cardinality defence, and it is applied here so no call site
        can forget it.
        """
        if len(values) != len(self.labelnames):
            raise ValueError(
                f"{self.name} takes {len(self.labelnames)} label(s), got {len(values)}."
            )

        if not self.allowed:
            return tuple(str(value) for value in values)

        return tuple(
            str(value) if (not permitted or str(value) in permitted) else OVERFLOW
            for value, permitted in zip(values, self.allowed, strict=True)
        )

    def _labels(self, values: LabelValues) -> str:
        if not values:
            return ""
        pairs = ",".join(
            f'{name}="{_escape(value)}"'
            for name, value in zip(self.labelnames, values, strict=True)
        )
        return "{" + pairs + "}"


class Counter(_Metric):
    """A value that only goes up."""

    kind = "counter"

    def __post_init__(self) -> None:
        super().__post_init__()
        self._values: dict[LabelValues, float] = {}

    def increment(self, *labels: str, amount: float = 1.0) -> None:
        if amount < 0:
            raise ValueError(f"{self.name} is a counter; it cannot decrease.")

        key = self._key(labels)
        with self._lock:
            if key not in self._values and len(self._values) >= _CEILING:
                key = (OVERFLOW,) * len(self.labelnames)
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, *labels: str) -> float:
        """What one series holds. For tests and for nothing else."""
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help_text}"
        yield f"# TYPE {self.name} counter"
        with self._lock:
            items = sorted(self._values.items())
        for key, total in items:
            yield f"{self.name}{self._labels(key)} {_format(total)}"


class Gauge(_Metric):
    """A value that goes up and down."""

    kind = "gauge"

    def __post_init__(self) -> None:
        super().__post_init__()
        self._values: dict[LabelValues, float] = {}

    def set(self, value: float, *labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            if key not in self._values and len(self._values) >= _CEILING:
                key = (OVERFLOW,) * len(self.labelnames)
            self._values[key] = value

    def value(self, *labels: str) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help_text}"
        yield f"# TYPE {self.name} gauge"
        with self._lock:
            items = sorted(self._values.items())
        for key, current in items:
            yield f"{self.name}{self._labels(key)} {_format(current)}"


@dataclass
class _Bucketed:
    counts: list[float]
    total: float = 0.0
    observations: float = 0.0


class Histogram(_Metric):
    """A distribution, in fixed buckets."""

    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...] = (),
        allowed: tuple[frozenset[str], ...] = (),
        buckets: Sequence[float] = DURATION_BUCKETS,
    ) -> None:
        super().__init__(name=name, help_text=help_text, labelnames=labelnames, allowed=allowed)
        self.buckets = tuple(sorted(buckets))
        self._series: dict[LabelValues, _Bucketed] = {}

    def observe(self, value: float, *labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            series = self._series.get(key)
            if series is None:
                if len(self._series) >= _CEILING:
                    key = (OVERFLOW,) * len(self.labelnames)
                    series = self._series.get(key)
                if series is None:
                    series = _Bucketed(counts=[0.0] * len(self.buckets))
                    self._series[key] = series

            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    series.counts[index] += 1
            series.total += value
            series.observations += 1

    def count(self, *labels: str) -> float:
        """How many observations one series holds. For tests."""
        with self._lock:
            series = self._series.get(self._key(labels))
            return series.observations if series else 0.0

    def sum(self, *labels: str) -> float:
        with self._lock:
            series = self._series.get(self._key(labels))
            return series.total if series else 0.0

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help_text}"
        yield f"# TYPE {self.name} histogram"

        with self._lock:
            items = sorted(self._series.items())

        for key, series in items:
            for bound, count in zip(self.buckets, series.counts, strict=True):
                labels = self._with(key, "le", _format(bound))
                yield f"{self.name}_bucket{labels} {_format(count)}"
            yield (
                f"{self.name}_bucket{self._with(key, 'le', '+Inf')} {_format(series.observations)}"
            )
            yield f"{self.name}_sum{self._labels(key)} {_format(series.total)}"
            yield f"{self.name}_count{self._labels(key)} {_format(series.observations)}"

    def _with(self, key: LabelValues, name: str, value: str) -> str:
        pairs = [
            f'{label}="{_escape(held)}"' for label, held in zip(self.labelnames, key, strict=True)
        ]
        pairs.append(f'{name}="{_escape(value)}"')
        return "{" + ",".join(pairs) + "}"


AnyMetric = Counter | Gauge | Histogram


class MetricRegistry:
    """Everything one application instance measures.

    An ordinary object rather than a module-level singleton, created with the
    application and discarded with it. Two applications in one process - which
    is every integration test - therefore measure independently.
    """

    def __init__(self) -> None:
        self._metrics: dict[str, AnyMetric] = {}

    def register(self, metric: AnyMetric) -> AnyMetric:
        if metric.name in self._metrics:
            raise ValueError(f"{metric.name} is already registered.")
        self._metrics[metric.name] = metric
        return metric

    def counter(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...] = (),
        allowed: tuple[frozenset[str], ...] = (),
    ) -> Counter:
        return cast(
            "Counter",
            self.register(
                Counter(name=name, help_text=help_text, labelnames=labelnames, allowed=allowed)
            ),
        )

    def gauge(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...] = (),
        allowed: tuple[frozenset[str], ...] = (),
    ) -> Gauge:
        return cast(
            "Gauge",
            self.register(
                Gauge(name=name, help_text=help_text, labelnames=labelnames, allowed=allowed)
            ),
        )

    def histogram(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...] = (),
        allowed: tuple[frozenset[str], ...] = (),
        buckets: Sequence[float] = DURATION_BUCKETS,
    ) -> Histogram:
        return cast(
            "Histogram",
            self.register(
                Histogram(
                    name=name,
                    help_text=help_text,
                    labelnames=labelnames,
                    allowed=allowed,
                    buckets=buckets,
                )
            ),
        )

    def get(self, name: str) -> AnyMetric | None:
        return self._metrics.get(name)

    def names(self) -> Sequence[str]:
        return sorted(self._metrics)

    def render(self) -> str:
        """The whole registry in Prometheus text exposition format."""
        lines: list[str] = []
        for name in sorted(self._metrics):
            lines.extend(self._metrics[name].render())
        return "\n".join(lines) + "\n"


def label_values(mapping: Mapping[str, str], names: Sequence[str]) -> tuple[str, ...]:
    """Pull label values out of a mapping in the order a metric declares them."""
    return tuple(mapping[name] for name in names)
