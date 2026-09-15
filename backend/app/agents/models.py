"""The agent domain: what an agent is, what a run is, and what a step records.

These are the runtime's own types, and they contain no database access - the
runtime talks to a :mod:`journal <app.agents.journal>` instead. What they are
*not* any more is the whole story: since the durable-execution phase the
authoritative record of a run is the ``agent_runs`` row, and an :class:`AgentRun`
here is the working copy of it for the length of one request.

Two things follow from that, and both are deliberate:

* :class:`AgentRunStatus` is the column type of ``agent_runs.status``. It is
  imported by ``app.models.agent_run`` rather than mirrored there, because two
  copies of a state machine drift and the one that drifts is the one nobody
  reads.
* The lifecycle is enforced here rather than by whoever calls it. A run that
  could be completed twice, or resumed after it failed, would make every later
  record of it untrustworthy - including the durable one.

Agent *definitions* still come from the server-side registry rather than the
``agents`` table; see :mod:`app.agents.registry`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.agents.decisions import AgentDecision, ToolRequestDecision
from app.agents.exceptions import AgentStateError
from app.ai.models import LLMMessage, LLMUsage
from app.tools.models import ToolResult

# Ceilings the application will not exceed whatever configuration says. A
# deployment can be stricter; it cannot be more permissive.
MAX_STEPS_CEILING = 32
MAX_OUTPUT_TOKENS_CEILING = 8_192


def _now() -> datetime:
    return datetime.now(UTC)


class Agent(BaseModel):
    """An agent definition.

    Frozen: a run must not be able to edit the agent it is running, and a
    shared registry entry must not be mutated by whoever borrowed it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1_000)

    instructions: str = Field(
        min_length=1,
        description="The system prompt. Server-controlled: no request field "
        "reaches this, so a caller cannot rewrite what the agent is.",
    )

    model: str | None = Field(
        default=None,
        description="Model to use. None means the deployment's configured "
        "model, which is the normal case.",
    )

    max_steps: int = Field(
        default=4,
        ge=0,
        le=MAX_STEPS_CEILING,
        description="How many model calls one run may make. Zero is legal and "
        "means the agent can do nothing - useful for disabling a run path "
        "without deleting the agent.",
    )
    max_output_tokens: int = Field(default=2_048, ge=1, le=MAX_OUTPUT_TOKENS_CEILING)
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="Left unset by default, so the provider's own default "
        "applies - which also avoids sending a sampling parameter to a model "
        "that rejects one.",
    )

    enabled: bool = True

    organization_id: uuid.UUID | None = Field(
        default=None,
        description="Owning tenant, or None for a platform-level agent every organization may run.",
    )

    def is_visible_to(self, organization_id: uuid.UUID) -> bool:
        """Whether *organization_id* may run this agent.

        A platform agent is visible to everyone; a tenant's agent only to that
        tenant. This is a domain rule, not the security boundary - the caller's
        organization itself comes from verified membership.
        """
        return self.organization_id is None or self.organization_id == organization_id


class AgentContext(BaseModel):
    """Everything one run is allowed to see.

    Tenant identity arrives already established: the API layer builds this from
    the caller's verified membership, so there is no path by which a request
    body could name the organization.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    run_id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    agent: Agent
    messages: tuple[LLMMessage, ...] = Field(
        description="The conversation so far, oldest first. User and assistant "
        "turns only - the system prompt comes from the agent."
    )
    request_id: str | None = Field(
        default=None,
        description="The HTTP correlation id, carried so a run can be found in "
        "the logs from the response the caller received.",
    )


class AgentStep(BaseModel):
    """One model call, and what it decided."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1, description="1 for the first call.")
    decision: AgentDecision
    model: str = Field(description="Model that served the call. Not the provider.")
    usage: LLMUsage
    latency_ms: float = Field(ge=0)
    started_at: datetime
    completed_at: datetime
    message_count: int = Field(ge=0, description="Messages sent, the system prompt included.")


class AgentToolCall(BaseModel):
    """One tool the agent asked for, and what the framework did about it.

    The arguments are not repeated here - they are on the step's decision. What
    this adds is the pairing: which step asked, and which execution answered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_number: int = Field(ge=1)
    tool_name: str
    result: ToolResult


class AgentRunStatus(enum.StrEnum):
    """Where a run is.

    Six states. `models.enums.RunStatus` is a near-twin but belongs to the
    persisted *workflow* tables and says "succeeded"; this one is the agent
    runtime's vocabulary and is what ``agent_runs.status`` stores.

    ``AWAITING_APPROVAL`` was added by the durable-execution phase, and is worth
    justifying because inventing lifecycle states is usually a mistake. Before
    approval was durable there was nobody to ask, so a tool that needed one
    simply came back refused and the run finished normally. Now a person really
    is being asked, and the run really is paused: it has done work, holds a
    conversation, has an execution waiting on a decision, and will continue
    afterwards. Recording that as ``completed`` would be a lie in the one record
    an operator is most likely to trust, and recording it as ``running`` would
    make it indistinguishable from a run whose request has died - which is
    exactly what the abandonment sweep looks for.
    """

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}
)
"""States a run never leaves. ``AWAITING_APPROVAL`` is deliberately not one."""

ACTIVE_STATUSES = frozenset({AgentRunStatus.PENDING, AgentRunStatus.RUNNING})
"""States in which a run is supposed to be making progress inside a request.

These are the ones the abandonment sweep considers: a run in either of them
whose record has not moved for longer than the configured window belongs to a
request that is no longer running. ``AWAITING_APPROVAL`` is excluded on purpose
- it is paused because somebody was asked, and age says nothing about it.
"""

# The only moves allowed. Anything absent from here is a bug, and raising on it
# is how that bug surfaces at the point it happens rather than as a confusing
# record later.
_ALLOWED_TRANSITIONS: dict[AgentRunStatus, frozenset[AgentRunStatus]] = {
    AgentRunStatus.PENDING: frozenset(
        {AgentRunStatus.RUNNING, AgentRunStatus.CANCELLED, AgentRunStatus.FAILED}
    ),
    AgentRunStatus.RUNNING: frozenset(
        {
            AgentRunStatus.AWAITING_APPROVAL,
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }
    ),
    # Back to running when a decision arrives, and out to a terminal state when
    # the run is abandoned or the request to resume it fails. There is no path
    # from here to completed: a paused run has not answered anything.
    AgentRunStatus.AWAITING_APPROVAL: frozenset(
        {AgentRunStatus.RUNNING, AgentRunStatus.CANCELLED, AgentRunStatus.FAILED}
    ),
    AgentRunStatus.COMPLETED: frozenset(),
    AgentRunStatus.FAILED: frozenset(),
    AgentRunStatus.CANCELLED: frozenset(),
}


class AgentRunCarryover(BaseModel):
    """What earlier requests of the same run already did.

    Zero for a run that is starting. Non-zero for one being resumed after an
    approval, where the steps, tools and tokens spent before the pause happened
    in a request that has long since returned - and must still count against the
    same budget, because the budget bounds the *run*, not one HTTP call.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0.0, ge=0)


class AgentRun(BaseModel):
    """One execution of one agent.

    Mutable, unlike everything else here: it is the record being built up as
    the run proceeds. Every change goes through a method that checks the
    transition first.

    Since the durable-execution phase this is the *working copy*. The
    authoritative record is the ``agent_runs`` row; this object is what one
    request holds while it advances that row, and what a resumed request rebuilds
    from it.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    agent_id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID

    status: AgentRunStatus = AgentRunStatus.PENDING
    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    steps: list[AgentStep] = Field(
        default_factory=list,
        description="Steps taken during *this* request. Steps from before a "
        "pause are counted by `carried`, not re-listed here.",
    )

    carried: AgentRunCarryover = Field(
        default_factory=AgentRunCarryover,
        description="Work done by earlier requests of the same run.",
    )

    final_response: str | None = Field(
        default=None,
        description="The answer, when the run produced one. None when it "
        "stopped on a tool request instead.",
    )
    tool_calls: list[AgentToolCall] = Field(
        default_factory=list,
        description="Every tool the agent asked for during the run, in order, "
        "with what the framework did about each.",
    )

    error_code: str | None = Field(
        default=None, description="Stable code of the failure, when one occurred."
    )
    error_message: str | None = Field(
        default=None,
        description="Client-safe description of the failure. Never a provider's own message.",
    )

    request_id: str | None = None

    pending_tool_execution_id: uuid.UUID | None = Field(
        default=None,
        description="The execution a person is being asked about, while the run "
        "is awaiting approval. Cleared when it resumes.",
    )

    # -- Derived ---------------------------------------------------------------

    @property
    def step_count(self) -> int:
        """Model calls this run has made, across every request that served it."""
        return self.carried.step_count + len(self.steps)

    @property
    def tool_call_count(self) -> int:
        return self.carried.tool_call_count + len(self.tool_calls)

    @property
    def usage(self) -> LLMUsage:
        """Every step's tokens added up.

        Summed from what the gateway reported. Nothing is recounted or
        estimated here - the provider's numbers are the only ones that matter
        for cost, and a second opinion would only ever be wrong.
        """
        return LLMUsage(
            input_tokens=self.carried.input_tokens
            + sum(step.usage.input_tokens for step in self.steps),
            output_tokens=self.carried.output_tokens
            + sum(step.usage.output_tokens for step in self.steps),
        )

    @property
    def latency_ms(self) -> float:
        """Time spent inside model calls, which is not the run's wall clock.

        A run that paused for approval does not accumulate the hours a person
        took to answer: only the model calls are counted, and the wait is
        visible from ``created_at`` and ``completed_at`` instead.
        """
        return self.carried.latency_ms + sum(step.latency_ms for step in self.steps)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_awaiting_approval(self) -> bool:
        return self.status is AgentRunStatus.AWAITING_APPROVAL

    # -- Transitions -----------------------------------------------------------

    def _transition(self, to: AgentRunStatus) -> None:
        if to not in _ALLOWED_TRANSITIONS[self.status]:
            raise AgentStateError(
                f"An agent run cannot move from {self.status.value} to {to.value}."
            )
        self.status = to

    def start(self) -> None:
        self._transition(AgentRunStatus.RUNNING)
        self.started_at = _now()

    def record(self, step: AgentStep) -> None:
        """Add a completed step. Only while running."""
        if self.status is not AgentRunStatus.RUNNING:
            raise AgentStateError("Steps can only be recorded while a run is running.")
        self.steps.append(step)

    def complete(self, *, final_response: str) -> None:
        """Finish with an answer."""
        self._transition(AgentRunStatus.COMPLETED)
        self.final_response = final_response
        self.completed_at = _now()

    def record_tool(self, request: ToolRequestDecision, result: ToolResult) -> None:
        """Record one tool execution. The run keeps going.

        Not a transition: a tool is something that happens *during* a run, and
        the run ends when the agent answers, runs out of budget, is cancelled or
        fails. Recording is refused outside the running state for the same
        reason steps are - a tool call attributed to a finished run would make
        the record untrustworthy.
        """
        if self.status is not AgentRunStatus.RUNNING:
            raise AgentStateError("Tool calls can only be recorded while a run is running.")

        self.tool_calls.append(
            AgentToolCall(
                step_number=self.step_count,
                tool_name=request.tool_name,
                result=result,
            )
        )

    def await_approval(self, *, tool_execution_id: uuid.UUID) -> None:
        """Pause, because a person has been asked about a tool.

        Not a failure and not an ending. The run keeps everything it has done;
        the execution named here is the one a decision will authorise, and it is
        the identity the tool will run under - at most once - if it is approved.
        """
        self._transition(AgentRunStatus.AWAITING_APPROVAL)
        self.pending_tool_execution_id = tool_execution_id

    def resume(self) -> None:
        """Carry on after a decision. Only from the paused state."""
        self._transition(AgentRunStatus.RUNNING)
        self.pending_tool_execution_id = None

    def fail(self, *, code: str, message: str) -> None:
        """Finish with a failure. *message* must be safe to show a client."""
        self._transition(AgentRunStatus.FAILED)
        self.error_code = code
        self.error_message = message
        self.completed_at = _now()

    def cancel(self) -> None:
        """Stop without an answer. Never produces a final response."""
        self._transition(AgentRunStatus.CANCELLED)
        self.completed_at = _now()
