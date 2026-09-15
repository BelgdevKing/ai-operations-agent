"""What a tool is, what it is asked, and what it answers.

Provider-independent and framework-only. Nothing here knows about Anthropic,
OpenAI, or any business system; nothing here reaches a network or a database.

The one idea worth stating plainly: **a request comes from a model and is
untrusted; a context is built by the server and is trusted.** They are separate
types so that no amount of carelessness inside a tool can turn one into the
other.
"""

from __future__ import annotations

import enum
import uuid
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.tools.summary import ApprovalSummary

# A tool name selects code, so it is an identifier rather than free text. Same
# shape the agent decision uses, checked again here because a registry can be
# populated from somewhere other than a model decision.
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

ToolName = Annotated[str, Field(pattern=TOOL_NAME_PATTERN)]

# Argument names the framework refuses whatever a tool's schema says. Identity
# is established by the execution context; a tool accepting any of these from
# its arguments would be taking instructions about who it is acting for from
# the model that called it.
RESERVED_ARGUMENT_NAMES = frozenset(
    {
        "organization_id",
        "organisation_id",
        "org_id",
        "tenant_id",
        "user_id",
        "actor_id",
        "run_id",
        "request_id",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "authorization",
        "credentials",
        "password",
        "secret",
    }
)


class ToolSafety(enum.StrEnum):
    """What running the tool does to the world.

    Three levels, on purpose. This exists so policy can be written against it -
    what may retry, what needs approval - not to be a permissions engine.
    """

    READ_ONLY = "read_only"
    """Looks something up. Running it twice changes nothing."""

    MUTATING = "mutating"
    """Changes state or has an outside effect: sends, creates, updates."""

    DESTRUCTIVE = "destructive"
    """Removes or cancels something. Hard or impossible to undo."""


class ToolOutcome(enum.StrEnum):
    """How an execution ended.

    Timed out and cancelled are separate from failed deliberately: one may have
    completed its side effect anyway, and the other was somebody's decision.

    ``REJECTED`` is separate for the same kind of reason, and it matters more.
    A person declining an action is not an infrastructure failure and must not
    be reported as one: nothing is broken, nothing should be retried, and the
    agent needs to be able to tell the user *why* it did not act. It is a
    provider-independent outcome the bounded loop reasons about like any other.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    APPROVAL_REQUIRED = "approval_required"
    REJECTED = "rejected"


class ToolMetadata(BaseModel):
    """Everything about a tool except how it works."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ToolName
    description: str = Field(
        min_length=1,
        max_length=1_000,
        description="What the tool does, in the words a model will read.",
    )

    safety: ToolSafety
    enabled: bool = True

    organization_scoped: bool = Field(
        default=True,
        description="Whether the tool acts on one tenant's data. True for "
        "anything touching business records - the conservative default, so a "
        "tool has to opt out of tenancy rather than remember to opt in.",
    )

    requires_approval: bool = Field(
        default=False,
        description="Whether a person must agree before it runs. The executor "
        "refuses one of these unless it is handed a decision that has already "
        "been recorded - it never grants its own.",
    )

    approval_summary: ApprovalSummary | None = Field(
        default=None,
        description="Which arguments an approver may be shown, and under what "
        "fixed phrase. Absent means the approval names the tool and nothing "
        "else, which is safe but tells a reviewer very little - so anything "
        "gated should declare one. The declaration is an allow-list: the "
        "arguments it does not name are never written to the approval record.",
    )

    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        le=300,
        description="Deadline for one execution. None uses the configured default.",
    )

    @model_validator(mode="after")
    def _destructive_tools_need_approval(self) -> Self:
        """Nothing destructive runs unattended.

        A policy rather than a preference: the cost of a wrongly-triggered
        cancellation is not symmetric with the cost of asking.
        """
        if self.safety is ToolSafety.DESTRUCTIVE and not self.requires_approval:
            raise ValueError("A destructive tool must require approval.")
        return self

    @model_validator(mode="after")
    def _summaries_belong_to_gated_tools(self) -> Self:
        """A summary describes what somebody is being asked to allow.

        A tool nobody is asked about has nothing to summarise, and a declaration
        on one is dead weight that would mislead the next reader into thinking
        the tool is gated.
        """
        if self.approval_summary is not None and not self.requires_approval:
            raise ValueError("Only a tool that requires approval may declare an approval summary.")
        return self

    @property
    def auto_execute(self) -> bool:
        """Whether the framework may run this without asking anyone."""
        return self.enabled and not self.requires_approval

    @property
    def retry_allowed(self) -> bool:
        """Whether re-running this on failure could ever be safe.

        Read-only work only. Nothing in the framework retries a tool today -
        this says which ones a future policy could consider, and the answer for
        anything with a side effect is no, because a retry would repeat it.
        """
        return self.safety is ToolSafety.READ_ONLY


class ToolRequest(BaseModel):
    """What a model asked for.

    **Untrusted.** Both fields came out of a language model. There is no
    identity here and there must never be: see `ToolExecutionContext`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionContext(BaseModel):
    """Who the execution is for.

    **Trusted.** Built by the server from an authenticated request and a
    verified membership, and passed to the tool alongside its arguments. A tool
    reads identity from here, never from what it was called with.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_execution_id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID

    run_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    request_id: str | None = Field(
        default=None,
        description="The HTTP correlation id, so one execution can be found "
        "from the response the caller received.",
    )


class ApprovalGrant(BaseModel):
    """A decision a person has already made about one execution.

    **The executor never creates one of these.** It is built by the approval
    service from a row a human decision wrote, and handed in - which is what
    keeps "may this run?" a question answered by the database and an
    authenticated administrator rather than by anything inside the agent loop.

    ``tool_execution_id`` is the identity the paused execution was given when it
    was refused. Carrying it back means an approved tool runs *as that
    execution*: the same id in the conversation, in the audit trail and in the
    ``tool_executions`` row that records at most one run of it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_execution_id: uuid.UUID
    granted: bool = Field(description="True to allow the execution, False for a refusal.")
    decided_by: uuid.UUID = Field(description="The administrator who decided.")


class ToolFailure(BaseModel):
    """Why an execution did not succeed, in terms safe to pass on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(description="Stable identifier, e.g. tool_invalid_arguments.")
    message: str = Field(description="Client-safe description. Never a library's own text.")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured context. Field names and constraint names only "
        "- never the values that failed, which are the caller's data.",
    )


class ToolResult(BaseModel):
    """The outcome of one execution.

    Always returned, whatever happened. The framework turns every failure into
    one of these rather than letting an exception travel into the agent
    runtime, so a run always has something to record.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_execution_id: uuid.UUID
    tool_name: str
    outcome: ToolOutcome

    data: dict[str, Any] | None = Field(
        default=None,
        description="The tool's output, validated against its declared schema "
        "and serialised. Present only when the execution succeeded.",
    )
    failure: ToolFailure | None = Field(
        default=None, description="Present for every outcome except success."
    )

    duration_ms: float = Field(default=0.0, ge=0)

    @property
    def ok(self) -> bool:
        return self.outcome is ToolOutcome.SUCCEEDED

    @model_validator(mode="after")
    def _outcome_matches_payload(self) -> Self:
        """A result cannot both succeed and carry a failure.

        Cheap to check and it makes every downstream branch trustworthy.
        """
        if self.ok and self.failure is not None:
            raise ValueError("A successful result cannot carry a failure.")
        if not self.ok and self.data is not None:
            raise ValueError("Only a successful result carries data.")
        return self
