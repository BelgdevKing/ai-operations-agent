"""Request and response schemas for running an agent.

Narrower than the generation endpoint's on purpose. That one lets a caller name
a model and a temperature, because it is a thin wrapper over a completion. An
agent is a *configured thing*, and its configuration is the server's - so the
request carries the conversation and nothing else.

What the body cannot contain, each enforced rather than documented:

* **A system message.** ``role`` accepts user and assistant only, so there is
  no system turn to smuggle in and no instructions to override.
* **A model, provider, temperature or token limit.** ``extra="forbid"`` means a
  body carrying one is a 422 rather than a silently ignored field.
* **An organization.** There is no field for it; the tenant comes from the
  caller's verified membership.
* **A run id, a status, or a tool result.** A client describes what it wants
  said, not what happened.

Since runs became durable, one more rule applies to the *response*: it is
projected from the stored run, never from whatever object a request happened to
hold. A replayed idempotent request therefore answers exactly as the original
did, and a run read back tomorrow reads the same as it did today.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.models import AgentRunStatus
from app.ai.models import LLMMessage, LLMRole
from app.models.conversation import Conversation, Message
from app.models.enums import MessageRole
from app.schemas.common import ApprovalField
from app.services.agent_execution import RunView
from app.tools.models import ToolOutcome

# Matches the runtime's own defaults. The runtime re-checks both against
# configuration, so these bound the payload and it bounds the run.
MAX_MESSAGES = 50
MAX_MESSAGE_CHARACTERS = 10_000
MAX_TOTAL_CHARACTERS = 50_000

# An idempotency key is an opaque client token. Bounded and restricted to
# printable ASCII so it fits its column and cannot smuggle control characters
# into a log line; never parsed for meaning.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:\-]{1,255}$"


class AgentMessage(BaseModel):
    """One turn of the conversation.

    ``system`` is absent from the role type deliberately - see the module
    docstring.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal[LLMRole.USER, LLMRole.ASSISTANT] = Field(
        description="user or assistant. The agent's own instructions are "
        "server-controlled and cannot be set here."
    )
    content: str = Field(
        min_length=1,
        max_length=MAX_MESSAGE_CHARACTERS,
        description=f"Message text, at most {MAX_MESSAGE_CHARACTERS:,} characters.",
    )

    def to_llm_message(self) -> LLMMessage:
        return LLMMessage(role=self.role, content=self.content)


class AgentRunRequest(BaseModel):
    """Ask an agent to handle a conversation."""

    model_config = ConfigDict(extra="forbid")

    messages: list[AgentMessage] = Field(
        min_length=1,
        max_length=MAX_MESSAGES,
        description=f"Conversation so far, oldest first. At most {MAX_MESSAGES} messages.",
    )

    conversation_id: uuid.UUID | None = Field(
        default=None,
        description="Continue a stored conversation. When set, `messages` must "
        "carry exactly one new user turn - the history is the server's and a "
        "client cannot rewrite it. When absent, a new conversation is started "
        "from everything sent.",
    )

    @model_validator(mode="after")
    def _limit_total_size(self) -> Self:
        total = sum(len(message.content) for message in self.messages)
        if total > MAX_TOTAL_CHARACTERS:
            raise ValueError(
                f"The conversation is {total:,} characters; the limit is {MAX_TOTAL_CHARACTERS:,}."
            )
        return self

    @model_validator(mode="after")
    def _continuing_sends_one_new_turn(self) -> Self:
        """A stored conversation may be added to, never rewritten.

        Checked here as well as in the service, because a 422 naming the rule is
        a better answer than a generic refusal - and because the service's copy
        is what protects any future caller that does not come through this
        schema.
        """
        if self.conversation_id is None:
            return self

        if len(self.messages) != 1 or self.messages[0].role is not LLMRole.USER:
            raise ValueError(
                "Continuing a conversation sends exactly one new user message; "
                "the history is the server's."
            )
        return self


class AgentUsage(BaseModel):
    """Tokens the whole run consumed, as the provider reported them."""

    model_config = ConfigDict(from_attributes=True)

    input_tokens: int
    output_tokens: int
    total_tokens: int


class ToolCallSummary(BaseModel):
    """One tool the agent used, as much as a client may see.

    Name and outcome only. The arguments and the returned data are the tenant's
    business information and the agent's working - a client gets the answer the
    agent reached, not the intermediate records it read.
    """

    model_config = ConfigDict(from_attributes=True)

    tool_name: str
    outcome: ToolOutcome | None = Field(
        default=None, description="How it ended. Null only while it has not."
    )


class PendingApproval(BaseModel):
    """What a paused run is waiting for.

    Enough for a person to understand the decision they are being asked to make:
    which tool, which record, why it is gated, when it was asked, who asked, and
    how long they have. **Not** the argument payload - what appears here is the
    projection the tool's own ``ApprovalSummary`` declared, taken once when the
    approval was requested, and there is no field on this model that could carry
    anything else.
    """

    id: uuid.UUID
    tool_name: str | None
    action: str
    reason: str | None
    summary: str | None = Field(
        default=None,
        description="What is being proposed, in one line. Built from the tool's "
        "own declaration of which fields a reviewer may see.",
    )
    summary_fields: list[ApprovalField] = Field(
        default_factory=list,
        description="The labelled values behind the summary. An allow-list the "
        "tool declared, never the argument payload.",
    )
    expires_at: datetime | None = Field(
        default=None, description="When this stops being decidable."
    )
    requested_at: datetime
    requested_by: uuid.UUID


class AgentRunResponse(BaseModel):
    """The outcome of one run, read from the durable record.

    Built from explicit fields, so nothing internal - a provider name, a raw
    response, an instructions string, an idempotency key - can reach a client
    through it.
    """

    run_id: uuid.UUID
    agent_id: uuid.UUID
    conversation_id: uuid.UUID | None
    status: AgentRunStatus

    final_response: str | None = Field(
        default=None,
        description="The answer, when the agent produced one. Null while the "
        "run is still going, waiting on a person, or ended without one.",
    )
    tool_calls: list[ToolCallSummary] = Field(
        default_factory=list,
        description="The tools the agent used to reach its answer, in order.",
    )
    approval: PendingApproval | None = Field(
        default=None,
        description="Set when the run is awaiting approval, and only then.",
    )

    error_code: str | None = Field(
        default=None,
        description="Stable code when the run failed, e.g. agent_run_abandoned.",
    )

    step_count: int = Field(description="Model calls the run made.")
    usage: AgentUsage
    latency_ms: float = Field(description="Time spent inside model calls, in milliseconds.")

    @classmethod
    def from_view(cls, view: RunView) -> AgentRunResponse:
        """Project a stored run onto the public contract."""
        record = view.record
        approval = view.pending_approval

        return cls(
            run_id=record.id,
            agent_id=record.agent_id,
            conversation_id=record.conversation_id,
            status=record.status,
            final_response=view.final_response,
            tool_calls=[
                ToolCallSummary(
                    tool_name=execution.tool_name,
                    outcome=ToolOutcome(execution.outcome) if execution.outcome else None,
                )
                for execution in view.tool_executions
            ],
            approval=(
                PendingApproval(
                    id=approval.id,
                    tool_name=approval.tool_name,
                    action=approval.action,
                    reason=approval.reason,
                    summary=approval.summary,
                    summary_fields=[ApprovalField(**field) for field in approval.summary_fields],
                    expires_at=approval.expires_at,
                    requested_at=approval.requested_at,
                    requested_by=approval.requested_by,
                )
                if approval is not None
                else None
            ),
            error_code=record.error_code,
            step_count=record.step_count,
            usage=AgentUsage(
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                total_tokens=record.input_tokens + record.output_tokens,
            ),
            latency_ms=float(record.latency_ms),
        )


class AgentSummary(BaseModel):
    """An agent, as a client is allowed to see it.

    ``instructions`` is absent: the system prompt is the server's, and showing
    it would hand a caller the text to work around.
    """

    id: uuid.UUID
    name: str
    description: str | None


# -- Stored conversations -----------------------------------------------------


class ConversationSummary(BaseModel):
    """A stored conversation, for a list."""

    id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, conversation: Conversation) -> ConversationSummary:
        return cls(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )


class ConversationTurn(BaseModel):
    """One turn of a stored conversation, as a client may see it.

    The two tool turns are deliberately reduced to a name and an outcome. A tool
    request's arguments and a tool result's payload are the tenant's business
    data, and publishing them so the interface can show its working is exactly
    the trade this codebase does not make. What the tools *found* reaches the
    reader through the agent's answer, which is the turn that was written for
    them.
    """

    id: uuid.UUID
    role: Literal["user", "assistant", "tool_request", "tool_result"]
    content: str | None = Field(
        default=None, description="Present for user and assistant turns only."
    )
    tool_name: str | None = None
    outcome: str | None = None
    created_at: datetime

    @classmethod
    def from_record(cls, message: Message) -> ConversationTurn:
        payload = message.tool_metadata or {}

        if message.role is MessageRole.TOOL:
            return cls(
                id=message.id,
                role="tool_result",
                tool_name=_text(payload.get("tool_name")),
                outcome=_text(payload.get("outcome")),
                created_at=message.created_at,
            )

        if message.role is MessageRole.ASSISTANT and payload.get("type") == "tool_request":
            return cls(
                id=message.id,
                role="tool_request",
                tool_name=_text(payload.get("tool_name")),
                created_at=message.created_at,
            )

        return cls(
            id=message.id,
            role="assistant" if message.role is MessageRole.ASSISTANT else "user",
            content=message.content,
            created_at=message.created_at,
        )


class ConversationDetail(BaseModel):
    """A stored conversation and its turns."""

    id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    turns: list[ConversationTurn]


def _text(value: object) -> str | None:
    return str(value) if isinstance(value, str) else None
