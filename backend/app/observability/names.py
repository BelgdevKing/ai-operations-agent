"""The metric vocabulary: every name, every label, every permitted value.

One file, so that "what can end up in a time series?" is a question with a
readable answer rather than a search. Everything a metric may be labelled with
is enumerated here and comes from a closed set the application itself defines -
an enum, a registered tool name, a configured model name, an error code written
in code.

Two label values are deliberately absent and must stay absent:

``organization_id``
    An operator scraping ``/metrics`` may legitimately watch the fleet without
    being entitled to any tenant's business data, and "which organization is
    spending the most" is exactly that. Per-tenant figures are a *usage query*,
    authenticated and tenant-scoped, not a metric.

anything per-execution
    ``run_id``, ``tool_execution_id``, ``approval_id``, ``request_id``. Each is
    unbounded, and unbounded labels are how a metrics process runs out of
    memory. They are excellent in logs, where they are what makes one request
    findable; they are indefensible here.
"""

from __future__ import annotations

# -- Metric names -------------------------------------------------------------
#
# One prefix, so a scrape can be filtered to this application in one expression.

HTTP_REQUESTS = "aiops_http_requests_total"
HTTP_DURATION = "aiops_http_request_duration_seconds"

AGENT_RUNS = "aiops_agent_runs_total"
AGENT_RUN_DURATION = "aiops_agent_run_duration_seconds"

LLM_CALLS = "aiops_llm_calls_total"
LLM_DURATION = "aiops_llm_call_duration_seconds"
LLM_TOKENS = "aiops_llm_tokens_total"
LLM_RETRIES = "aiops_llm_retries_total"

TOOL_EXECUTIONS = "aiops_tool_executions_total"
TOOL_DURATION = "aiops_tool_execution_duration_seconds"

WORKFLOW_RUNS = "aiops_workflow_runs_total"
WORKFLOW_RUN_DURATION = "aiops_workflow_run_duration_seconds"
WORKFLOW_STEPS = "aiops_workflow_steps_total"

APPROVAL_DECISIONS = "aiops_approval_decisions_total"
APPROVAL_WAIT = "aiops_approval_wait_seconds"

ERRORS = "aiops_errors_total"

# -- Label vocabularies -------------------------------------------------------

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
"""The methods this API serves. Anything else is somebody probing."""

STATUS_CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx"})
"""The status *class*, not the code.

A class is four values and answers the question a dashboard asks. The exact
code is in the access log, where one request can actually be looked up.
"""

LLM_OPERATIONS = frozenset({"generate", "generate_structured"})
"""The two things the gateway does."""

CALL_OUTCOMES = frozenset({"success", "failed"})
"""How a call ended, from the caller's point of view. Retries are their own
counter: an attempt that was retried and then succeeded is one successful call,
not two calls."""

# The lifecycle vocabularies, written out rather than derived from the enums
# that define them.
#
# **This module imports nothing from the rest of the application, on purpose.**
# Observability is cross-cutting infrastructure: it is imported *by* the agent
# runtime, the tool framework and the workflow engine, so importing them back
# is a cycle - and the first attempt at this file proved it, by failing to
# start. Literals keep the package a leaf.
#
# The cost of writing them out is that a new enum member could be forgotten
# here. That is paid for in `tests/unit/test_metric_names.py`, which asserts
# each set below equals its enum exactly - so the duplication is checked by a
# test rather than trusted to a reader. And the failure mode if it were ever
# missed is benign: an unlisted value is recorded as `other` rather than
# admitted as a new series.

AGENT_RUN_STATUSES = frozenset(
    {"pending", "running", "awaiting_approval", "completed", "failed", "cancelled"}
)
"""``app.agents.models.AgentRunStatus``."""

WORKFLOW_RUN_STATUSES = frozenset(
    {"pending", "running", "awaiting_approval", "succeeded", "failed", "cancelled"}
)
"""``app.models.enums.RunStatus`` - note "succeeded" where the agent says "completed"."""

WORKFLOW_STEP_STATUSES = frozenset(
    {
        "pending",
        "running",
        "awaiting_approval",
        "succeeded",
        "failed",
        "skipped",
        "cancelled",
    }
)
"""``app.models.enums.StepRunStatus``."""

WORKFLOW_STEP_TYPES = frozenset({"tool_call", "agent_step", "approval", "condition"})
"""``app.models.enums.WorkflowStepType``."""

APPROVAL_DECISIONS_SET = frozenset({"pending", "approved", "rejected", "expired", "cancelled"})
"""``app.models.enums.ApprovalStatus``."""

TOOL_OUTCOMES = frozenset(
    {"succeeded", "failed", "timed_out", "cancelled", "approval_required", "rejected"}
)
"""``app.tools.models.ToolOutcome``."""

TOOL_SAFETY = frozenset({"read_only", "mutating", "destructive", "unknown"})
"""``app.tools.models.ToolSafety``, plus "unknown" for a tool the registry has
no metadata for - which the executor can legitimately encounter when a model
names a tool that does not exist."""

TOKEN_DIRECTIONS = frozenset({"input", "output"})

LAYERS = frozenset({"http", "agent", "llm", "tool", "workflow", "approval"})
"""Where an error was counted. Bounded by the architecture's own layer names."""

# ``model`` and ``tool`` are the two labels whose values are not enumerable in
# code: a model name comes from deployment configuration and a tool name from
# the registry. Both are bounded by the deployment rather than by a user - no
# request body can introduce a new one - and the registry's overflow bucket
# catches anything unexpected regardless. An empty set means "not enumerated
# here"; it does not mean "unchecked", because MAX_SERIES_PER_METRIC still
# applies.
UNENUMERATED: frozenset[str] = frozenset()


def status_class(status_code: int) -> str:
    """``503`` -> ``5xx``. Out-of-range codes fall into ``5xx``."""
    first = status_code // 100
    return f"{first}xx" if 2 <= first <= 5 else "5xx"
