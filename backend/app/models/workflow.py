"""Workflows: versioned definitions, their runs, and each step of a run.

The schema phase created these four tables and nothing wrote to them. The
workflow phase writes to three of them.

``workflows``
    One row per **version** of a definition. The steps live in ``definition``,
    a validated JSONB document - see :mod:`app.workflows.definition`. Data, not
    code: it names tools, agents, comparisons and transitions, and there is no
    field in it that can express an expression, a callable or a module.

``workflow_runs``
    One execution. Durable and authoritative: the engine holds a working copy
    for the length of a request and this row is what survives it.

``workflow_step_runs``
    One row per step of a run, with what that step produced and what it links
    to - an agent run, a tool execution, an approval. ``UNIQUE(workflow_run_id,
    step_key)`` is what makes "a step cannot execute twice" a property of the
    database rather than of the code path that happens to be running.

``workflow_steps``
    Left as the schema phase created it, and still unwritten. It models a
    definition as numbered rows, which a branching graph is not; the definition
    document replaced it. ``workflow_step_runs.workflow_step_id`` is therefore
    nullable now, in the same way and for the same reason as
    ``conversations.agent_id``: the thing it points at lives somewhere the table
    cannot reference.

Tenancy is the Part 14 pattern throughout - composite foreign keys that carry
the organization, so a run of another tenant's workflow, or a step of another
tenant's run, is refused by the database rather than filtered by a query::

    workflow_runs      (workflow_id, organization_id)     -> workflows(id, organization_id)
    workflow_step_runs (workflow_run_id, organization_id) -> workflow_runs(id, organization_id)
    workflow_step_runs (agent_run_id, organization_id)    -> agent_runs(id, organization_id)
    tool_executions    (workflow_step_run_id, organization_id)
                                                          -> workflow_step_runs(id, organization_id)
    approvals          (workflow_run_id, organization_id) -> workflow_runs(id, organization_id)

**A security note about ``output_data``.** It holds what each step produced,
because that is the workflow's data context and a later step has to be able to
read it. For a tool step that means business records. This is the same split the
durable-execution phase drew for conversations: the *operational* tables
(``tool_executions``, ``agent_steps``) hold counts and codes and never the
payload, and exactly one place holds the content. Here that place is
``workflow_step_runs``, it is tenant-scoped, and it is bounded by
``workflow_max_output_bytes``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import (
    RunStatus,
    StepRunStatus,
    WorkflowStatus,
    WorkflowStepType,
    enum_column,
)
from app.models.mixins import OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent_run import AgentRunRecord
    from app.models.organization import Organization
    from app.models.user import User

# A client-chosen token, stored as given and never interpreted. Same shape and
# same reasoning as the agent run's.
IDEMPOTENCY_KEY_MAX_LENGTH = 255

# Step ids come from the definition, where they are identifiers.
STEP_KEY_MAX_LENGTH = 64

FIRST_VERSION = 1


class Workflow(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """One version of a named procedure belonging to one organization.

    A new version is a new row, which is what lets a running execution keep
    using the definition it started with while somebody edits the next one.
    Nothing mutates a definition in place except the status.
    """

    __tablename__ = "workflows"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=FIRST_VERSION, server_default=text("1")
    )

    status: Mapped[WorkflowStatus] = mapped_column(
        enum_column(WorkflowStatus, "workflow_status"),
        nullable=False,
        default=WorkflowStatus.DRAFT,
        server_default=WorkflowStatus.DRAFT.value,
    )

    # The steps, as a validated document. JSONB rather than rows because the
    # graph branches: `workflow_steps` numbers steps in a line, which cannot
    # express "on_true go here, on_false go there".
    definition: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    organization: Mapped[Organization] = relationship(viewonly=True)

    steps: Mapped[list[WorkflowStep]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="WorkflowStep.step_order",
    )

    __table_args__ = (
        # A name identifies a procedure; a version identifies which edition of
        # it. Both together are what must be unique.
        UniqueConstraint(
            "organization_id", "name", "version", name="uq_workflows_organization_name_version"
        ),
        # Referenced by the runs' composite key.
        UniqueConstraint("id", "organization_id", name="uq_workflows_id_organization_id"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_workflows_organization_id_status", "organization_id", "status"),
        Index("ix_workflows_organization_id_name", "organization_id", "name"),
    )


class WorkflowStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One step of a workflow definition, as the schema phase modelled it.

    **Unused.** Kept rather than dropped: it is somebody's existing table, the
    workflow phase found a shape that suits a branching graph better, and
    removing a table is not a change to make on the way past. See the module
    docstring.
    """

    __tablename__ = "workflow_steps"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    step_type: Mapped[WorkflowStepType] = mapped_column(
        enum_column(WorkflowStepType, "step_type"),
        nullable=False,
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)

    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    workflow: Mapped[Workflow] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("workflow_id", "step_order", name="uq_workflow_steps_workflow_id_order"),
        CheckConstraint("step_order >= 0", name="step_order_non_negative"),
        Index("ix_workflow_steps_workflow_id", "workflow_id"),
    )


class WorkflowRun(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """One execution of one version of a workflow.

    ``workflow_version`` is recorded as well as ``workflow_id`` even though the
    id already names a version row. It is what makes the audit trail readable
    without a join, and it is the field somebody looks at when asking "which
    edition of this process did that run follow?".

    ``updated_at`` is what makes abandonment detectable: every state change
    touches this row, so a ``running`` run whose row has not moved for longer
    than the configured window belongs to a request that is no longer running.
    """

    __tablename__ = "workflow_runs"

    workflow_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    workflow_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=FIRST_VERSION, server_default=text("1")
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # RESTRICT: who started a process is part of the record.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus, "status"),
        nullable=False,
        default=RunStatus.PENDING,
        server_default=RunStatus.PENDING.value,
    )

    # Which step the run is at, by its id in the definition. Null once the run
    # has finished.
    current_step: Mapped[str | None] = mapped_column(String(STEP_KEY_MAX_LENGTH), nullable=True)

    step_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # What the run was started with, and what it ended up with. Both are the
    # workflow's own data context - see the module docstring.
    input_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(
        String(IDEMPOTENCY_KEY_MAX_LENGTH), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # The client-safe sentence an error class defines. Never a provider's, a
    # driver's or a tool's own words.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship(viewonly=True)
    user: Mapped[User] = relationship(viewonly=True)
    workflow: Mapped[Workflow] = relationship(viewonly=True)

    # Read-only: a step run is written by the engine with its own tenant and
    # position, never by cascading a list assignment through the parent. The
    # database still removes them with the run - that is the composite key's
    # ON DELETE CASCADE, not this relationship's job.
    step_runs: Mapped[list[WorkflowStepRun]] = relationship(
        back_populates="workflow_run",
        order_by="WorkflowStepRun.position",
        viewonly=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["workflow_id", "organization_id"],
            ["workflows.id", "workflows.organization_id"],
            ondelete="CASCADE",
            name="fk_workflow_runs_workflow_id_organization_id_workflows",
        ),
        # The idempotency guarantee, held by the database rather than by
        # anything this process remembers. NULL keys never collide.
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_workflow_runs_organization_id_idempotency_key",
        ),
        UniqueConstraint("id", "organization_id", name="uq_workflow_runs_id_organization_id"),
        CheckConstraint("step_count >= 0", name="step_count_not_negative"),
        CheckConstraint("workflow_version >= 1", name="workflow_version_positive"),
        Index("ix_workflow_runs_workflow_id_organization_id", "workflow_id", "organization_id"),
        Index("ix_workflow_runs_organization_id_status", "organization_id", "status"),
        Index("ix_workflow_runs_organization_id_created_at", "organization_id", "created_at"),
        # The abandonment sweep: unfinished runs, oldest activity first.
        Index("ix_workflow_runs_status_updated_at", "status", "updated_at"),
        Index("ix_workflow_runs_user_id", "user_id"),
    )


class WorkflowStepRun(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """One step's execution within a run.

    One row per step, not one per attempt. The engine does not retry steps - a
    tool that changes something must not be re-run on a guess, and the tool
    framework already decides what is safe to repeat - so a second row for the
    same step would only ever mean the same work happened twice. The uniqueness
    constraint makes that impossible rather than unlikely.

    Carries ``organization_id`` of its own, unlike the schema phase's version of
    this table. It is what the composite keys to the run, the agent run and the
    tool execution are built from, and it is what makes a cross-tenant step row
    unrepresentable.
    """

    __tablename__ = "workflow_step_runs"

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    # The step's id in the definition document. This, not workflow_step_id, is
    # what identifies a step now.
    step_key: Mapped[str] = mapped_column(String(STEP_KEY_MAX_LENGTH), nullable=False)

    step_type: Mapped[WorkflowStepType] = mapped_column(
        enum_column(WorkflowStepType, "step_type"), nullable=False
    )

    # Execution order, from 1. A total order that ``created_at`` cannot give:
    # PostgreSQL holds ``now()`` constant for a transaction, so rows written
    # together would share a timestamp and sort by a random primary key.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    # Nullable since the definition became a document - see the module
    # docstring. Nothing writes it.
    workflow_step_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_steps.id", ondelete="CASCADE"),
        nullable=True,
    )

    status: Mapped[StepRunStatus] = mapped_column(
        enum_column(StepRunStatus, "status"),
        nullable=False,
        default=StepRunStatus.PENDING,
        server_default=StepRunStatus.PENDING.value,
    )

    # Nullable rather than defaulting to {}: "no output yet" and "produced an
    # empty result" are different facts, and a resumed run needs to tell them
    # apart.
    input_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Where an agent step's work actually happened, so one request id leads to
    # the workflow run, to the step, to the agent run underneath.
    #
    # The tool and approval links point the other way - `tool_executions` and
    # `approvals` each carry `workflow_step_run_id` - because a column here as
    # well would make the two tables reference each other, and a cycle of
    # foreign keys is a thing every future migration has to be careful about for
    # no benefit. Each link is stored once, in the direction that already
    # existed for agent runs.
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship(viewonly=True)
    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="step_runs", viewonly=True)
    step: Mapped[WorkflowStep | None] = relationship(viewonly=True)
    agent_run: Mapped[AgentRunRecord | None] = relationship(viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workflow_run_id", "organization_id"],
            ["workflow_runs.id", "workflow_runs.organization_id"],
            ondelete="CASCADE",
            name="fk_workflow_step_runs_workflow_run_id_organization_id_runs",
        ),
        ForeignKeyConstraint(
            ["agent_run_id", "organization_id"],
            ["agent_runs.id", "agent_runs.organization_id"],
            ondelete="SET NULL",
            name="fk_workflow_step_runs_agent_run_id_organization_id_agent_runs",
        ),
        # One row per step of a run. This is what "a step cannot execute twice"
        # means: a duplicate request, a retried HTTP call or a second resume
        # collides here rather than doing the work again.
        UniqueConstraint(
            "workflow_run_id", "step_key", name="uq_workflow_step_runs_run_id_step_key"
        ),
        UniqueConstraint(
            "workflow_run_id", "position", name="uq_workflow_step_runs_run_id_position"
        ),
        UniqueConstraint("id", "organization_id", name="uq_workflow_step_runs_id_organization_id"),
        CheckConstraint("position >= 1", name="position_positive"),
        Index(
            "ix_workflow_step_runs_workflow_run_id_organization_id",
            "workflow_run_id",
            "organization_id",
        ),
        Index("ix_workflow_step_runs_organization_id_status", "organization_id", "status"),
        Index("ix_workflow_step_runs_workflow_step_id", "workflow_step_id"),
    )
