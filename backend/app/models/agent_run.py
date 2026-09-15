"""Durable agent execution: runs, steps and the tool executions inside them.

Three tables, and one rule that shapes all of them: **they hold operational
metadata, not content.** What the user asked, what the model answered and what
a tool returned live in ``conversations``/``messages``. What lives here is which
run, which step, which tool, how it ended, how long it took and how much it
cost - the record an operator needs to explain a run without reading anyone's
business data.

That split is deliberate and worth stating plainly, because the obvious design
is to put everything in one place:

    tool_executions   ->  operational metadata only
    messages          ->  conversation content, including tool turns

So no tool arguments, no tool results, no prompts, no completions and no
exception text appear in any column below. ``error_code`` and ``error_message``
carry the *class-level* code and client-safe sentence the API already returns -
never a driver's or a provider's own words.

Tenancy is enforced the way Part 14 enforces it, with composite foreign keys
that carry the organization::

    agent_runs       (conversation_id, organization_id) -> conversations(id, organization_id)
    agent_steps      (run_id, organization_id)          -> agent_runs(id, organization_id)
    tool_executions  (run_id, organization_id)          -> agent_runs(id, organization_id)

A step belonging to one tenant cannot be attached to another tenant's run: the
database refuses the row rather than the application forgetting to filter it.

``agent_runs.agent_id`` is deliberately **not** a foreign key. Agent definitions
come from the server-side :class:`~app.agents.registry.AgentRegistry`, not from
the ``agents`` table: the platform agent every tenant can run has no owning
organization, which ``agents.organization_id NOT NULL`` cannot represent. A key
would therefore force either a redesign of the runtime or a per-tenant copy of a
platform agent under a different id in every organization - which would make the
id the API already publishes untrue. The column records the agent that ran, in
the same spirit as ``audit_events.resource_id``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

# The runtime's own lifecycle enum is the single source of truth for what a run
# can be. Imported rather than mirrored here: two copies of a state machine
# drift, and the one that drifts is always the one nobody is reading.
from app.agents.models import AgentRunStatus
from app.models.base import Base
from app.models.enums import enum_column
from app.models.mixins import (
    CreatedAtMixin,
    OrganizationScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

if TYPE_CHECKING:
    from app.models.conversation import Conversation
    from app.models.organization import Organization
    from app.models.user import User

# An idempotency key is a client-chosen token, so it is bounded and stored as
# given. Never interpreted, never parsed, never logged.
IDEMPOTENCY_KEY_MAX_LENGTH = 255

# Long enough for the client-safe sentences the error classes define, short
# enough that nothing tempts a caller to put a stack trace in it.
ERROR_MESSAGE_MAX_LENGTH = 500

TOOL_NAME_MAX_LENGTH = 64


class AgentRunRecord(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """One durable agent run.

    The authoritative record of an execution. The in-memory
    :class:`~app.agents.models.AgentRun` is the working copy for the duration of
    one request; this row is what survives it, and what a later request reads to
    decide whether the same work has already been done.

    ``updated_at`` is what makes abandonment detectable: every state change and
    every recorded step touches this row, so a ``running`` run whose row has not
    moved for longer than the configured window is a run whose request died.
    """

    __tablename__ = "agent_runs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # RESTRICT: who started a run is part of the record.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Not a foreign key - see the module docstring.
    agent_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    # Nullable so a run can exist before its conversation does, and so deleting
    # a conversation does not have to delete the operational record first.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    status: Mapped[AgentRunStatus] = mapped_column(
        enum_column(AgentRunStatus, "agent_run_status"),
        nullable=False,
        default=AgentRunStatus.PENDING,
        server_default=AgentRunStatus.PENDING.value,
    )

    # Client-supplied, from the Idempotency-Key header. Unique per organization
    # and NULL when absent - PostgreSQL treats NULLs as distinct, so runs made
    # without a key never collide with each other.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(IDEMPOTENCY_KEY_MAX_LENGTH), nullable=True
    )

    # The HTTP correlation id of the request that created the run. Only ever
    # the first one: a resumed run keeps the id it was born with, so the whole
    # execution stays findable from the response the user originally saw.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    step_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    tool_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # Time spent inside model calls, not the run's wall clock - and emphatically
    # not the hours a person may have taken to answer an approval.
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(
        String(ERROR_MESSAGE_MAX_LENGTH), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship(viewonly=True)
    user: Mapped[User] = relationship(viewonly=True)
    conversation: Mapped[Conversation | None] = relationship(viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            ondelete="CASCADE",
            name="fk_agent_runs_conversation_id_organization_id_conversations",
        ),
        # The idempotency guarantee, enforced by the database rather than by
        # anything this process remembers. Two concurrent requests carrying the
        # same key for the same organization contend here, and exactly one wins.
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_agent_runs_organization_id_idempotency_key",
        ),
        # Referenced by the composite keys on steps, tool executions and
        # approvals. Redundant with the primary key alone; it is what makes a
        # cross-tenant child row unrepresentable.
        UniqueConstraint("id", "organization_id", name="uq_agent_runs_id_organization_id"),
        CheckConstraint("step_count >= 0", name="step_count_not_negative"),
        CheckConstraint("tool_call_count >= 0", name="tool_call_count_not_negative"),
        # The run list, and the approval-aware status filters behind it.
        Index("ix_agent_runs_organization_id_status", "organization_id", "status"),
        Index("ix_agent_runs_organization_id_created_at", "organization_id", "created_at"),
        Index(
            "ix_agent_runs_conversation_id_organization_id",
            "conversation_id",
            "organization_id",
        ),
        # The abandonment sweep: unfinished runs, oldest activity first.
        Index("ix_agent_runs_status_updated_at", "status", "updated_at"),
        Index("ix_agent_runs_user_id", "user_id"),
    )


class AgentStepRecord(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
    """One model call inside a run.

    Immutable, so it carries only ``created_at``. Records *that* a decision was
    made and what kind - never the text of it. A final answer's content is the
    conversation's; a tool request's arguments are the conversation's too.
    """

    __tablename__ = "agent_steps"

    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    step_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # "final" or "tool_request", from app.agents.decisions.DecisionType. Stored
    # as a plain string rather than a constrained enum: the runtime's union is
    # designed to gain members, and a historical row must stay readable when it
    # does.
    decision_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # Present when the decision asked for a tool. The name only.
    tool_name: Mapped[str | None] = mapped_column(String(TOOL_NAME_MAX_LENGTH), nullable=True)

    # The model that served the call. Not the provider: which vendor answered is
    # internal routing and is deliberately absent from every outward contract.
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship(viewonly=True)
    run: Mapped[AgentRunRecord] = relationship(viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "organization_id"],
            ["agent_runs.id", "agent_runs.organization_id"],
            ondelete="CASCADE",
            name="fk_agent_steps_run_id_organization_id_agent_runs",
        ),
        # A step number identifies a step within its run. Writing the same one
        # twice - a retry, a resumed request replaying work - is refused by the
        # database instead of producing a run that appears to have done more
        # than it did.
        UniqueConstraint("run_id", "step_number", name="uq_agent_steps_run_id_step_number"),
        CheckConstraint("step_number >= 1", name="step_number_positive"),
        Index("ix_agent_steps_run_id_organization_id", "run_id", "organization_id"),
        Index("ix_agent_steps_organization_id_created_at", "organization_id", "created_at"),
    )


class ToolExecutionRecord(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """One tool the agent asked for, and what the framework did about it.

    ``id`` is the ``tool_execution_id`` the tool framework already assigns, so
    the identity in the conversation, in the logs and in this row is one value.
    That is what makes "the tool ran at most once for this execution" a
    statement about a specific row rather than about a timing window.

    ``executed`` is the at-most-once flag. It is set by a conditional update -
    ``SET executed = true WHERE executed = false`` - so two concurrent approvals
    of the same execution cannot both reach the tool body.

    No arguments and no results: both are the tenant's business data, and both
    are already in the conversation, which is the store that is allowed to hold
    them. What is kept here is how many arguments there were and how many fields
    came back, which is enough to explain a call without disclosing one.
    """

    __tablename__ = "tool_executions"

    # Exactly one of these is set: a tool is run either by an agent run or by a
    # workflow step, and this table is the platform's single record of every
    # tool it has run. The check constraint below is what makes "exactly one"
    # true rather than customary.
    run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    workflow_step_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    # Which step of its caller asked. An agent run numbers its model calls; a
    # workflow step run has exactly one execution, so this is 1 for those.
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)

    tool_name: Mapped[str] = mapped_column(String(TOOL_NAME_MAX_LENGTH), nullable=False)

    # ToolSafety: read_only, mutating or destructive. Why the call was gated, in
    # one column, without having to resolve the registry to find out.
    safety: Mapped[str | None] = mapped_column(String(16), nullable=True)

    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # Whether the tool body actually ran. False for a refused, pending or
    # rejected execution - the flag the at-most-once guarantee turns on.
    executed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # ToolOutcome, or NULL while the execution is waiting on a person.
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)

    argument_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    result_field_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship(viewonly=True)
    run: Mapped[AgentRunRecord | None] = relationship(viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "organization_id"],
            ["agent_runs.id", "agent_runs.organization_id"],
            ondelete="CASCADE",
            name="fk_tool_executions_run_id_organization_id_agent_runs",
        ),
        ForeignKeyConstraint(
            ["workflow_step_run_id", "organization_id"],
            ["workflow_step_runs.id", "workflow_step_runs.organization_id"],
            ondelete="CASCADE",
            name="fk_tool_executions_workflow_step_run_id_organization_id_steps",
        ),
        # One execution per workflow step. The workflow engine's at-most-once
        # guarantee, in the same place the agent runtime's lives.
        UniqueConstraint("workflow_step_run_id", name="uq_tool_executions_workflow_step_run_id"),
        CheckConstraint(
            "(run_id IS NULL) <> (workflow_step_run_id IS NULL)",
            name="exactly_one_caller",
        ),
        # One tool execution per step of a run. The step number comes from the
        # model call that asked for it, so a replayed or duplicated resume
        # cannot create a second execution for work already recorded.
        UniqueConstraint("run_id", "step_number", name="uq_tool_executions_run_id_step_number"),
        # Referenced by approvals, so an approval cannot point at another
        # tenant's execution.
        UniqueConstraint("id", "organization_id", name="uq_tool_executions_id_organization_id"),
        CheckConstraint("step_number >= 1", name="step_number_positive"),
        CheckConstraint("argument_count >= 0", name="argument_count_not_negative"),
        Index("ix_tool_executions_run_id_organization_id", "run_id", "organization_id"),
        Index("ix_tool_executions_organization_id_tool_name", "organization_id", "tool_name"),
        Index("ix_tool_executions_organization_id_created_at", "organization_id", "created_at"),
    )
