"""The metrics this application actually keeps, and the only way to record them.

A registry on its own is a box of counters; this is the lid. Every measurement
goes through a named method here rather than through ``counter.increment(...)``
at a call site, and that has one purpose: **a call site cannot get the labels
wrong, because it never sees them.** ``record_tool_execution(tool, outcome,
safety, seconds)`` has no argument that could accidentally be a tenant id.

The instruments are built per application instance, not per process. That is
what makes "this counter incremented exactly once" a testable statement: an
integration test constructs its own application and reads its own numbers.

Nothing here touches the database, the network or a provider SDK. Recording a
measurement is a dictionary update behind an uncontended lock, so a service can
call it inside a request without thinking about it.
"""

from __future__ import annotations

from app.observability import names
from app.observability.metrics import (
    WAIT_BUCKETS,
    Counter,
    Histogram,
    MetricRegistry,
)


class Instruments:
    """Every metric this application keeps, and typed ways to record them."""

    def __init__(self, registry: MetricRegistry | None = None) -> None:
        self.registry = registry or MetricRegistry()
        build = self.registry

        # -- HTTP --------------------------------------------------------------
        self.http_requests: Counter = build.counter(
            names.HTTP_REQUESTS,
            "HTTP requests served, by method, route template and status class.",
            ("method", "route", "status"),
            (names.HTTP_METHODS, names.UNENUMERATED, names.STATUS_CLASSES),
        )
        self.http_duration: Histogram = build.histogram(
            names.HTTP_DURATION,
            "Time to serve an HTTP request, in seconds.",
            ("method", "route"),
            (names.HTTP_METHODS, names.UNENUMERATED),
        )

        # -- Agent runs --------------------------------------------------------
        self.agent_runs: Counter = build.counter(
            names.AGENT_RUNS,
            "Agent runs that reached a state, by that state.",
            ("status",),
            (names.AGENT_RUN_STATUSES,),
        )
        self.agent_run_duration: Histogram = build.histogram(
            names.AGENT_RUN_DURATION,
            "Time one request spent advancing an agent run, in seconds.",
            ("status",),
            (names.AGENT_RUN_STATUSES,),
        )

        # -- Model calls -------------------------------------------------------
        self.llm_calls: Counter = build.counter(
            names.LLM_CALLS,
            "Model calls, by model, operation and outcome.",
            ("model", "operation", "outcome"),
            (names.UNENUMERATED, names.LLM_OPERATIONS, names.CALL_OUTCOMES),
        )
        self.llm_duration: Histogram = build.histogram(
            names.LLM_DURATION,
            "Time one model call took, in seconds, as the gateway measured it.",
            ("model", "operation"),
            (names.UNENUMERATED, names.LLM_OPERATIONS),
        )
        self.llm_tokens: Counter = build.counter(
            names.LLM_TOKENS,
            "Tokens reported by the provider, by model and direction.",
            ("model", "direction"),
            (names.UNENUMERATED, names.TOKEN_DIRECTIONS),
        )
        self.llm_retries: Counter = build.counter(
            names.LLM_RETRIES,
            "Model call attempts retried, by the error class that caused it.",
            ("error_code",),
            (names.UNENUMERATED,),
        )

        # -- Tools -------------------------------------------------------------
        self.tool_executions: Counter = build.counter(
            names.TOOL_EXECUTIONS,
            "Tool executions, by tool, outcome and safety classification.",
            ("tool", "outcome", "safety"),
            (names.UNENUMERATED, names.TOOL_OUTCOMES, names.TOOL_SAFETY),
        )
        self.tool_duration: Histogram = build.histogram(
            names.TOOL_DURATION,
            "Time one tool execution took, in seconds.",
            ("tool",),
            (names.UNENUMERATED,),
        )

        # -- Workflows ---------------------------------------------------------
        self.workflow_runs: Counter = build.counter(
            names.WORKFLOW_RUNS,
            "Workflow runs that reached a state, by that state.",
            ("status",),
            (names.WORKFLOW_RUN_STATUSES,),
        )
        self.workflow_run_duration: Histogram = build.histogram(
            names.WORKFLOW_RUN_DURATION,
            "Time one request spent advancing a workflow run, in seconds.",
            ("status",),
            (names.WORKFLOW_RUN_STATUSES,),
        )
        self.workflow_steps: Counter = build.counter(
            names.WORKFLOW_STEPS,
            "Workflow steps that finished, by kind and state.",
            ("step_type", "status"),
            (names.WORKFLOW_STEP_TYPES, names.WORKFLOW_STEP_STATUSES),
        )

        # -- Approvals ---------------------------------------------------------
        self.approval_decisions: Counter = build.counter(
            names.APPROVAL_DECISIONS,
            "Approvals that left the pending state, by how they left it.",
            ("decision",),
            (names.APPROVAL_DECISIONS_SET,),
        )
        self.approval_wait: Histogram = build.histogram(
            names.APPROVAL_WAIT,
            "Time an approval waited for a person, in seconds.",
            ("decision",),
            (names.APPROVAL_DECISIONS_SET,),
            buckets=WAIT_BUCKETS,
        )

        # -- Errors ------------------------------------------------------------
        self.errors: Counter = build.counter(
            names.ERRORS,
            "Errors, by the stable code an exception class defines and the layer.",
            ("error_code", "layer"),
            (names.UNENUMERATED, names.LAYERS),
        )

    # -- Recording -------------------------------------------------------------
    #
    # One method per thing worth measuring. Durations arrive in milliseconds
    # because that is what every record in this codebase already stores, and
    # are converted here - so no call site has to remember which unit a
    # histogram wants.

    def record_http(self, *, method: str, route: str, status_code: int, seconds: float) -> None:
        self.http_requests.increment(method, route, names.status_class(status_code))
        self.http_duration.observe(seconds, method, route)

    def record_agent_run(self, *, status: str, milliseconds: float) -> None:
        self.agent_runs.increment(status)
        self.agent_run_duration.observe(milliseconds / 1000.0, status)

    def record_llm_call(
        self,
        *,
        model: str,
        operation: str,
        outcome: str,
        milliseconds: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.llm_calls.increment(model, operation, outcome)
        self.llm_duration.observe(milliseconds / 1000.0, model, operation)

        # Zero-token increments are skipped rather than recorded: a counter
        # that is incremented by nothing still creates the series, and a
        # deployment whose provider reports no usage should show no token
        # series at all rather than a row of zeroes that looks like data.
        if input_tokens:
            self.llm_tokens.increment(model, "input", amount=float(input_tokens))
        if output_tokens:
            self.llm_tokens.increment(model, "output", amount=float(output_tokens))

    def record_llm_retry(self, *, error_code: str) -> None:
        self.llm_retries.increment(error_code)

    def record_tool_execution(
        self, *, tool: str, outcome: str, safety: str | None, milliseconds: float
    ) -> None:
        self.tool_executions.increment(tool, outcome, safety or "unknown")
        self.tool_duration.observe(milliseconds / 1000.0, tool)

    def record_workflow_run(self, *, status: str, milliseconds: float) -> None:
        self.workflow_runs.increment(status)
        self.workflow_run_duration.observe(milliseconds / 1000.0, status)

    def record_workflow_step(self, *, step_type: str, status: str) -> None:
        self.workflow_steps.increment(step_type, status)

    def record_approval_decision(self, *, decision: str, waited_seconds: float | None) -> None:
        self.approval_decisions.increment(decision)
        if waited_seconds is not None and waited_seconds >= 0:
            self.approval_wait.observe(waited_seconds, decision)

    def record_error(self, *, error_code: str, layer: str) -> None:
        self.errors.increment(error_code, layer)

    def render(self) -> str:
        return self.registry.render()


class NullInstruments(Instruments):
    """Instruments that keep nothing.

    The default wherever a component may be built without an application -
    which is every unit test, and every service constructed directly. Following
    the same pattern as ``NULL_JOURNAL`` in the agent runtime: silent rather
    than raising, because a component that is not being measured is a
    legitimate configuration and not an error.

    A subclass rather than a protocol so a caller cannot be handed something
    that is *almost* an ``Instruments``; and it still builds a real registry, so
    a test that does want to read numbers can.
    """

    def render(self) -> str:
        return ""
