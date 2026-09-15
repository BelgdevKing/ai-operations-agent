"""Human approval requests for sensitive agent actions.

One table, extended rather than duplicated. It was created by the schema phase
and carried most of what a durable approval needs already - who asked, what for,
which status, who decided and when. The durable-execution phase added the three
columns that tie an approval to a specific execution, and the workflow phase
added two more for the other kind of process that pauses::

    run_id                which agent run is paused
    tool_execution_id     which execution the decision authorises
    tool_name             what is being asked for, without resolving the registry
    workflow_run_id       which workflow run is paused
    workflow_step_run_id  which step of it

An approval belongs to exactly one process: an agent run or a workflow run,
never both. Every link carries the organization in a composite foreign key, so
an approval in one tenant cannot reference another tenant's run, step or
execution. That, plus the
tenant-scoped repository, is why a cross-tenant approval is not merely filtered
out but impossible to store.

**``parameters`` is not used by agent approvals and must stay empty.** It was
designed to hold the exact arguments an action was requested with. Tool
arguments are business data: they belong in the conversation, which is the
content store, and the durable record of an approved execution is the
``tool_execution_id`` - so approving replays the original call by its identity
rather than by copying its arguments into a second place. A workflow approval
may still use the column; nothing the agent writes does, and a test asserts it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.agent_run import AgentRunRecord, ToolExecutionRecord
from app.models.base import Base
from app.models.enums import ApprovalStatus, enum_column
from app.models.mixins import OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent import Agent, Tool
    from app.models.conversation import Conversation
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.workflow import WorkflowRun


class Approval(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """A request for a human to authorise an action before it runs."""

    __tablename__ = "approvals"

    requested_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # RESTRICT: who asked is part of the record and must not disappear.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # All of these are nullable: an approval can come from a workflow step
    # rather than an agent run, and the action need not involve a registered
    # tool. An agent approval always sets run_id, tool_execution_id and
    # tool_name; a workflow approval sets none of them.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=True,
    )
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tools.id", ondelete="RESTRICT"),
        nullable=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
    )

    # The paused run. Composite-keyed with the organization below.
    run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    # The execution this decision authorises. The tool runs at most once for
    # this identity, whatever happens to the approval afterwards.
    tool_execution_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    # The paused workflow run and the step within it. Set for a workflow
    # approval, null for an agent one - the two are different processes asking
    # the same question, and an approval belongs to exactly one of them.
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    workflow_step_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )

    # The tool's name, so an approver can be shown what is being asked for
    # without the registry being consulted and without the arguments being read.
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    action: Mapped[str] = mapped_column(String(200), nullable=False)

    # Left empty by agent approvals - see the module docstring.
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    # Why the action was gated, shown to the approver.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[ApprovalStatus] = mapped_column(
        enum_column(ApprovalStatus, "status"),
        nullable=False,
        default=ApprovalStatus.PENDING,
        server_default=ApprovalStatus.PENDING.value,
    )

    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship()
    requester: Mapped[User] = relationship(foreign_keys=[requested_by])
    approver: Mapped[User | None] = relationship(foreign_keys=[approved_by])
    agent: Mapped[Agent | None] = relationship()
    tool: Mapped[Tool | None] = relationship()
    conversation: Mapped[Conversation | None] = relationship()
    run: Mapped[AgentRunRecord | None] = relationship(viewonly=True)
    tool_execution: Mapped[ToolExecutionRecord | None] = relationship(viewonly=True)
    workflow_run: Mapped[WorkflowRun | None] = relationship(viewonly=True)

    __table_args__ = (
        # Tenant-carrying keys, the Part 14 pattern: an approval for another
        # organization's run or execution is refused by the database.
        ForeignKeyConstraint(
            ["run_id", "organization_id"],
            ["agent_runs.id", "agent_runs.organization_id"],
            ondelete="CASCADE",
            name="fk_approvals_run_id_organization_id_agent_runs",
        ),
        ForeignKeyConstraint(
            ["tool_execution_id", "organization_id"],
            ["tool_executions.id", "tool_executions.organization_id"],
            ondelete="CASCADE",
            name="fk_approvals_tool_execution_id_organization_id_tool_executions",
        ),
        ForeignKeyConstraint(
            ["workflow_run_id", "organization_id"],
            ["workflow_runs.id", "workflow_runs.organization_id"],
            ondelete="CASCADE",
            name="fk_approvals_workflow_run_id_organization_id_workflow_runs",
        ),
        ForeignKeyConstraint(
            ["workflow_step_run_id", "organization_id"],
            ["workflow_step_runs.id", "workflow_step_runs.organization_id"],
            ondelete="CASCADE",
            name="fk_approvals_workflow_step_run_id_organization_id_step_runs",
        ),
        # One approval per workflow step. A duplicated request - a retried
        # pause, a resumed run asking again - collides here rather than putting
        # a second decision in front of a person.
        UniqueConstraint("workflow_step_run_id", name="uq_approvals_workflow_step_run_id"),
        # One approval per execution. A duplicated approval request - a retried
        # pause, a resumed run asking again - collides here rather than putting
        # a second decision in front of a person.
        UniqueConstraint("tool_execution_id", name="uq_approvals_tool_execution_id"),
        # The approval queue: pending items for one organization.
        Index("ix_approvals_organization_id_status", "organization_id", "status"),
        Index("ix_approvals_requested_by", "requested_by"),
        Index("ix_approvals_approved_by", "approved_by"),
        Index("ix_approvals_agent_id", "agent_id"),
        Index("ix_approvals_tool_id", "tool_id"),
        Index("ix_approvals_conversation_id", "conversation_id"),
        Index("ix_approvals_run_id_organization_id", "run_id", "organization_id"),
        Index(
            "ix_approvals_workflow_run_id_organization_id",
            "workflow_run_id",
            "organization_id",
        ),
        Index(
            "ix_approvals_tool_execution_id_organization_id",
            "tool_execution_id",
            "organization_id",
        ),
        # Sweeping expired requests looks only at pending ones.
        Index("ix_approvals_status_expires_at", "status", "expires_at"),
    )
